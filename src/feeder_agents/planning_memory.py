"""Project-local diagnostic episodes; verified observations, never new authority.

Planning verification and physical delivery verification are separate levels.
Numeric targets are intentionally excluded from retrieval keys, but included in
episode identity. Every reuse still passes the current request's validators.
"""
import copy
import hashlib
import importlib.metadata
import json
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .artifacts import atomic_json, digest, file_digest


def compatibility_signature():
    import pydantic
    from .hierarchy import HierarchicalSpec
    from .transmission import TransmissionSpec
    from .requirement_contract import CAPABILITIES
    code=Path(__file__).parent
    versions={}
    for name in ('opendssdirect.py','dss-python','numpy','pypower','scipy'):
        try:versions[name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:versions[name]='not installed'
    # JSON schemas omit custom validators. Bind the actual implementation,
    # equipment catalogues and solvers used to establish a verification level.
    sources=sorted(code.rglob('*.py'))+sorted((code/'data').rglob('*.json'))
    return digest(dict(version='m53',code={str(p.relative_to(code)):file_digest(p) for p in sources},
        runtime=versions,pydantic=pydantic.__version__,
        schemas=[HierarchicalSpec.model_json_schema(),TransmissionSpec.model_json_schema()],capabilities=CAPABILITIES))


def match_context(bundle):
    from .planning_diagnostics import normalize_candidate
    from .requirement_contract import canonical
    candidate=normalize_candidate(bundle.get('candidate'))
    family=candidate.get('family')
    spec=candidate.get('single_voltage_spec' if family=='single_voltage' else 'distribution_spec' if family=='hierarchical' else 'transmission_spec') or {}
    if not isinstance(spec,dict):spec={}
    features=[]
    for fact in bundle['facts']:
        features.append(dict(field=canonical(fact.get('field') or ''),operator=fact.get('operator'),
            location=[v for v in fact.get('location',[]) if not isinstance(v,int)],error_type=fact.get('error_type')))
    # Do not match by natural-language similarity alone, or transfer a physical
    # MV/LV context into a transmission or different-voltage context.
    return dict(family=family,code=bundle['code'],stage=bundle['stage'],
        scene=spec.get('scenario',{}).get('kind','urban') if family=='single_voltage' else spec.get('scene','urban') if family=='hierarchical' else None,
        voltage_kv=spec.get('voltage_kv',10 if family in ('hierarchical','single_voltage') else 220),
        lv_voltage_kv=spec.get('lv_voltage_kv',.4) if family=='hierarchical' else None,
        voltage_layers=sorted(v.get('kv') for v in spec.get('voltage_layers',[]) or [] if isinstance(v,dict)),
        features=sorted({json.dumps(f,sort_keys=True) for f in features}))


def _read(path):return json.loads(Path(path).read_text())


def _valid_files(files):
    try:return all(file_digest(Path(p))==h for p,h in files.items())
    except OSError:return False


def _replay_planning(record, final):
    """Reproduce deterministic failure and verify a claimed successful repair."""
    from pydantic import ValidationError
    from .adaptive_planning import AdaptiveProposal, _compile
    from .planning_diagnostics import failure_bundle, check_adjustment, PlanningValidationError
    from .structured_planning import StructuredPlanningError
    from .source_review import check_source_numbers
    request=record['request']
    if digest(request)!=record['request_hash']:raise ValueError('Source request hash differs')
    failure=None
    try:
        proposal=AdaptiveProposal.model_validate(copy.deepcopy(record['candidate']))
        before=_compile(proposal,request)
        if before['status']=='ready':check_source_numbers(request,before)
    except ValidationError as exc:failure=StructuredPlanningError(str(exc),exc)
    except PlanningValidationError as exc:failure=exc
    if failure is None:raise ValueError('Initial failure cannot be independently reproduced by deterministic checks')
    fresh=failure_bundle(failure,record['stage'],request,record['candidate'])
    if match_context(fresh)!=match_context(record):raise ValueError('Saved failure facts differ from replay')
    decision=record['decision']
    if set(decision['evidence_ids'])!={f['id'] for f in fresh['facts']}:raise ValueError('Unverified evidence references')
    action=next((a for a in fresh['allowed_actions'] if a['id']==decision['action_id']),None)
    if action is None:raise ValueError('Unverified action')
    after=record.get('adjustment',{}).get('candidate')
    status=record.get('verification',{}).get('status')
    if status not in ('passed','failed') or after is None:return 'unverified'
    try:
        check_adjustment(record['candidate'],after,action,request=request)
        repaired=AdaptiveProposal.model_validate(copy.deepcopy(after))
        supplied=record.get('adjustment',{}).get('single_voltage_input')
        if repaired.family=='single_voltage' and isinstance(supplied,dict):
            repaired._single_voltage_input=copy.deepcopy(supplied)
        compiled=_compile(repaired,request)
        if compiled['status']!='ready':raise ValueError('Correction did not produce a ready plan')
        check_source_numbers(request,compiled)
    except ValueError:
        if status=='failed':return 'failed_adjustment'
        raise
    if status=='failed':return 'unverified'  # e.g. semantic reviewer disagreed; not a proven bad action.
    if not final or final.get('status')!='ready' or final.get('request')!=request:
        raise ValueError('Missing final source-bound planning evidence')
    if compiled['plan']!=final['plan'] or compiled['requirement_ledger']!=final['requirement_ledger']:
        raise ValueError('Final plan differs from the repaired candidate')
    if not final.get('semantic_review',{}).get('approved'):
        raise ValueError('Successful adjustment lacks independent source review')
    return 'planning_verified'


class PlanningMemory:
    def __init__(self,path,mode='learn'):
        if mode not in ('learn','read_only'):raise ValueError('Unknown planning memory mode')
        self.path=Path(path).resolve();self.mode=mode
        self.frozen=False
        if self.path.is_file():
            with self._db() as db:
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='planning_meta'").fetchone():
                    self.frozen=bool(db.execute("SELECT 1 FROM planning_meta WHERE key='frozen' AND value='1'").fetchone())
            if self.frozen and mode=='learn':raise ValueError('Frozen memory must be opened read_only')
        if mode=='learn':
            self.path.parent.mkdir(parents=True,exist_ok=True)
            with self._db(write=True) as db:
                db.execute('CREATE TABLE IF NOT EXISTS planning_episodes '
                    '(id TEXT PRIMARY KEY, version TEXT, match_key TEXT, status TEXT, payload TEXT, payload_hash TEXT, invalid_reason TEXT)')
                db.execute('CREATE INDEX IF NOT EXISTS planning_lookup ON planning_episodes(version,match_key)')
                db.execute('CREATE TABLE IF NOT EXISTS planning_evidence (hash TEXT PRIMARY KEY, content BLOB)')
                db.execute('CREATE TABLE IF NOT EXISTS planning_meta (key TEXT PRIMARY KEY, value TEXT)')
        elif not self.path.is_file():raise ValueError('Read-only memory snapshot does not exist')

    @contextmanager
    def _db(self,write=False):
        if write and self.mode!='learn':raise ValueError('Read-only memory cannot be updated')
        db=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=5) if self.mode=='read_only' else sqlite3.connect(self.path,timeout=5)
        try:
            with db:yield db
        finally:db.close()

    def _files_valid(self,files):
        if not self.frozen:return _valid_files(files)
        with self._db() as db:
            for checksum in set(files.values()):
                row=db.execute('SELECT content FROM planning_evidence WHERE hash=?',(checksum,)).fetchone()
                if row is None or hashlib.sha256(row[0]).hexdigest()!=checksum:return False
        return True

    def ingest_run(self,root,final_path=None):
        if self.mode=='read_only':return dict(recorded=0,rejected=[],read_only=True)
        root=Path(root).resolve()
        final_path=Path(final_path).resolve() if final_path else root/'planning_result.json'
        final=_read(final_path) if final_path.is_file() else None
        source=root/'source_contract.json'
        source_data=_read(source)
        report=dict(recorded=0,renewed=0,rejected=[])
        version=compatibility_signature()
        for path in sorted((root/'failure_analysis').glob('attempt_*.json')):
            record=_read(path)
            if not record.get('decision'):continue
            try:
                if source_data!=dict(request=record['request'],request_hash=record['request_hash']):
                    raise ValueError('Episode source differs from original contract')
                status=_replay_planning(record,final)
            except (ValueError,KeyError,TypeError) as exc:
                report['rejected'].append(dict(artifact=str(path),reason=str(exc)))
                continue
            context=match_context(record)
            verification=record.get('verification',{})
            changes=[]
            for change in record.get('adjustment',{}).get('changes',[]):
                # Complete objects remain in source artifacts; prompt examples stay bounded.
                c=copy.deepcopy(change)
                for side in ('before','after'):
                    if len(json.dumps(c.get(side),ensure_ascii=False))>400:c[side]='See source artifact for structured change'
                changes.append(c)
            files={str(path):file_digest(path),str(source):file_digest(source)}
            if final is not None:files[str(final_path)]=file_digest(final_path)
            identity=dict(version=version,request=record['request_hash'],before=record['candidate'],
                after=record.get('adjustment',{}).get('candidate'),action=record['decision']['action_id'],status=status)
            key=digest(identity)
            payload=dict(id=key,root=str(root),context=context,action_id=record['decision']['action_id'],
                facts=record['facts'],reason=record['decision']['reason'],expected_change=record['decision']['expected_change'],
                changes=changes[:12],verification_error=verification.get('error'),files=files,
                source_model=(final or {}).get('interpreter_model'),created_utc=datetime.now(timezone.utc).isoformat())
            with self._db(write=True) as db:
                old=db.execute('SELECT payload FROM planning_episodes WHERE id=?',(key,)).fetchone()
                if old:
                    payload['created_utc']=json.loads(old[0])['created_utc']
                    payload['last_verified_utc']=datetime.now(timezone.utc).isoformat()
                    # Refresh replayed evidence without treating duplicates as
                    # independent successes or undoing manual invalidation.
                    db.execute('UPDATE planning_episodes SET status=?,payload=?,payload_hash=? WHERE id=?',
                        (status,json.dumps(payload,ensure_ascii=False),digest(payload),key))
                    report['renewed']+=1
                else:
                    db.execute('INSERT INTO planning_episodes VALUES (?,?,?,?,?,?,NULL)',
                        (key,version,digest(context),status,json.dumps(payload,ensure_ascii=False),digest(payload)))
                    report['recorded']+=1
        return report

    def retrieve(self,bundle,limit=4):
        if not 1<=limit<=8:raise ValueError('Memory limit must be between 1 and 8')
        allowed={a['id'] for a in bundle['allowed_actions']}
        with self._db() as db:
            rows=db.execute('SELECT id,status,payload,payload_hash FROM planning_episodes WHERE version=? '
                'AND match_key=? AND invalid_reason IS NULL ORDER BY rowid DESC LIMIT 200',
                (compatibility_signature(),digest(match_context(bundle)))).fetchall()
        groups={}
        for key,status,raw,checksum in rows:
            p=json.loads(raw)
            if digest(p)!=checksum or not self._files_valid(p['files']) or p['action_id'] not in allowed:continue
            if status not in ('planning_verified','delivery_verified','failed_adjustment'):continue
            # A stale delivery proof does not invalidate a still-valid planning repair.
            delivery=status=='delivery_verified' and self._files_valid(p.get('delivery_files',{})) and bool(p.get('delivery_files'))
            level='delivery_verified' if delivery else 'planning_verified' if status=='delivery_verified' else status
            g=groups.setdefault(p['action_id'],dict(action_id=p['action_id'],observations=0,planning_successes=0,
                delivery_successes=0,failed_adjustments=0,examples=[]))
            g['observations']+=1;g['planning_successes']+=int(level!='failed_adjustment')
            g['delivery_successes']+=int(delivery);g['failed_adjustments']+=int(level=='failed_adjustment')
            if len(g['examples'])<3:
                g['examples'].append(dict(episode_id=key,verification_level=level,source=p['root'],
                    reason=p['reason'],expected_change=p['expected_change'],changes=p['changes'],
                    verification_error=p['verification_error']))
        for g in groups.values():
            g['smoothed_success_rate']=(g['planning_successes']+1)/(g['observations']+2)
            g['usage']='Historical observations only. Use current facts/targets; failed adjustments do not prohibit this action. Revalidate every new candidate.'
        return sorted(groups.values(),key=lambda g:(-g['smoothed_success_rate'],-g['delivery_successes'],g['action_id']))[:limit]

    def verify_delivery(self,planner_root,brief,output,final_path=None):
        """Promote only the same plan after fresh export reload and electrical checks."""
        if self.mode=='read_only':return 0
        root=Path(planner_root).resolve()
        saved=_read(final_path or root/'planning_result.json')
        if any(saved.get(k)!=brief.get(k) for k in ('request','plan','plan_type','requirement_ledger')):
            raise ValueError('Delivery brief differs from the verified planning result')
        from .requirements import RequirementLedger
        from .requirement_contract import audit_delivery, delivered_formats
        from .hierarchy import HierarchicalFeeder
        from .transmission import TransmissionSpec, validate_case
        import numpy as np
        directory=Path(output['directory']).resolve()
        manifest=_read(directory/'manifest.json')
        if manifest.get('spec')!=brief['plan']['spec']:
            raise ValueError('Delivery manifest differs from the verified planning spec')
        from .hierarchy_workflow import read_hierarchy_result
        from .transmission import read_transmission_result
        from .workflow import read_verified_experiment
        readers={'hierarchical':read_hierarchy_result,'transmission':read_transmission_result,'feeder':read_verified_experiment}
        if brief['plan_type'] not in readers:raise ValueError('Unsupported delivery verification family')
        checked=readers[brief['plan_type']](directory)
        if any(checked.get(k)!=output.get(k) for k in ('result_hash','configuration_hash','attempted','accepted','samples','experiment_id')):
            raise ValueError('Delivery output differs from authenticated artifacts')
        count=manifest['spec']['count']
        ids=[s['sample_id'] for s in output['samples']]
        if output['attempted']!=count or len(ids)!=count or set(ids)!={f'sample_{i:05d}' for i in range(count)}:
            raise ValueError('Delivery manifest batch is incomplete')
        if not audit_delivery(RequirementLedger.model_validate(brief['requirement_ledger']),brief,output)['all_satisfied']:return 0
        for row in output['samples']:
            folder=directory/row['sample_id']
            if not folder.resolve().is_relative_to(directory):raise ValueError('Sample escaped delivery directory')
            if brief['plan_type']=='hierarchical':
                network=HierarchicalFeeder.model_validate(_read(folder/'feeder.json'))
                if 'opendss' not in delivered_formats(folder,brief,network):return 0
            elif brief['plan_type']=='feeder':
                from .schemas import Feeder
                network=Feeder.model_validate(_read(folder/'feeder.json'))
                required=set(brief['plan'].get('export_formats',['opendss']))
                if not required<=set(delivered_formats(folder,brief,network)):return 0
            else:
                network=_read(folder/'case.json')
                for key in ('bus','gen','branch'):network[key]=np.asarray(network[key])
                if 'matpower' not in delivered_formats(folder,brief,network):return 0
                validation,_=validate_case(network,TransmissionSpec.model_validate(brief['plan']['spec']),_read(folder/'metadata.json'))
                if not validation['accepted']:return 0
        files={str(p):file_digest(p) for p in directory.rglob('*') if p.is_file() and p.suffix in ('.json','.dss','.m')}
        promoted=0
        with self._db(write=True) as db:
            rows=db.execute('SELECT id,payload,payload_hash FROM planning_episodes WHERE version=? AND status=? AND invalid_reason IS NULL',
                            (compatibility_signature(),'planning_verified')).fetchall()
            for key,raw,checksum in rows:
                p=json.loads(raw)
                if p['root']!=str(root) or digest(p)!=checksum or not _valid_files(p['files']):continue
                p['delivery_files']=files;p['delivery_verified_utc']=datetime.now(timezone.utc).isoformat()
                db.execute('UPDATE planning_episodes SET status=?,payload=?,payload_hash=? WHERE id=?',
                           ('delivery_verified',json.dumps(p,ensure_ascii=False),digest(p),key))
                promoted+=1
        return promoted

    def invalidate(self,key,reason):
        if not reason.strip():raise ValueError('Invalidation requires a reason')
        with self._db(write=True) as db:db.execute('UPDATE planning_episodes SET invalid_reason=? WHERE id=?',(reason,key))

    def stats(self):
        with self._db() as db:
            rows=db.execute('SELECT status,COUNT(*) FROM planning_episodes GROUP BY status').fetchall()
            invalid=db.execute('SELECT COUNT(*) FROM planning_episodes WHERE invalid_reason IS NOT NULL').fetchone()[0]
        counts=dict(rows)
        return dict(episodes=sum(counts.values()),invalidated=invalid,**{s:counts.get(s,0) for s in (
            'planning_verified','delivery_verified','failed_adjustment','unverified')})

    def snapshot(self,path):
        path=Path(path).resolve()
        if path.exists():raise ValueError('Snapshot already exists')
        path.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent,suffix='.sqlite',delete=False) as stream:
            temporary=Path(stream.name)
        try:
            with self._db() as source:
                target=sqlite3.connect(temporary)
                try:
                    source.backup(target)
                    with target:
                        target.execute('CREATE TABLE IF NOT EXISTS planning_evidence (hash TEXT PRIMARY KEY, content BLOB)')
                        target.execute('CREATE TABLE IF NOT EXISTS planning_meta (key TEXT PRIMARY KEY, value TEXT)')
                        for key,raw,checksum,invalid in target.execute('SELECT id,payload,payload_hash,invalid_reason FROM planning_episodes').fetchall():
                            p=json.loads(raw)
                            if invalid:continue
                            if digest(p)!=checksum:raise ValueError('Changed memory payload: '+key)
                            # A snapshot includes proof bytes, so its eligibility
                            # cannot change when original run folders disappear.
                            for name,h in {**p['files'],**p.get('delivery_files',{})}.items():
                                existing=target.execute('SELECT content FROM planning_evidence WHERE hash=?',(h,)).fetchone()
                                content=existing[0] if existing else Path(name).read_bytes()
                                if hashlib.sha256(content).hexdigest()!=h:raise ValueError('Changed memory evidence: '+name)
                                target.execute('INSERT OR IGNORE INTO planning_evidence VALUES (?,?)',(h,content))
                        target.execute("INSERT OR REPLACE INTO planning_meta VALUES ('frozen','1')")
                finally:target.close()
            temporary.replace(path)
        finally:temporary.unlink(missing_ok=True)
        atomic_json(path.with_suffix('.provenance.json'),dict(source=str(self.path),sha256=file_digest(path),
            compatibility=compatibility_signature(),stats=self.stats(),mode='Use read_only during evaluation'))
        return path
