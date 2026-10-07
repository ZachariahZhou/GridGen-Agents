"""Evidence-backed, context-isolated feedback experience; never grants permissions."""
import json
import importlib.metadata
import sqlite3
from collections import defaultdict
from pathlib import Path
from .artifacts import digest,file_digest


def signature(spec):
    from .rules import data
    code=Path(__file__).parent
    return digest(dict(solvers={n:importlib.metadata.version(n) for n in ('opendssdirect.py','dss-python','numpy')},spec=spec.model_dump(exclude={'seed','count'}),
        code={p.name:file_digest(p) for p in sorted(code.glob('*.py'))},
        catalogues={n:digest(data(n+'.json')) for n in ('hierarchy_equipment','lv_conductors','lv_underground','equipment_joint','conductors')}))


class VerifiedMemory:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS episodes (id TEXT PRIMARY KEY, signature TEXT, payload TEXT, invalid_reason TEXT)')

    def invalidate(self,episode_id,reason):
        if not reason:raise ValueError('Invalidation requires a reason')
        with sqlite3.connect(self.path) as db:db.execute('UPDATE episodes SET invalid_reason=? WHERE id=?',(reason,episode_id))

    def ingest(self,root,spec,selected):
        from .hierarchy import HierarchicalFeeder
        from .hierarchy_feedback import verify_saved_feedback,assessment,apply_feedback_action,feedback_contract
        from .hierarchy_inverse import acceptable_step
        root=Path(root).resolve()
        verify_saved_feedback(root,selected,spec)
        report=json.loads((root/'result.json').read_text());contract=json.loads((root/'contract.json').read_text())
        def load(name):
            import re
            if not re.fullmatch(r'candidate_[0-9]{4}',name):raise ValueError('Invalid candidate identifier')
            return HierarchicalFeeder.model_validate(json.loads((root/name/'feeder.json').read_text()))
        base=load('candidate_0000');events=[];current_id='candidate_0000'
        protection=contract.get('protect_customer_allocation',False)
        for row in report['trace']:
            if 'candidate' not in row or 'error' in row:continue
            if row['parent']!=current_id:raise ValueError('Experience parent differs from accepted history')
            before=load(row['parent']);after=load(row['candidate'])
            replay=apply_feedback_action(before,row['action'],spec,contract['allowed_actions'],contract.get('local_topology',False),contract.get('mv_structure',False),protection)
            if digest(replay.model_dump())!=digest(after.model_dump()) or not feedback_contract(base,after,spec,contract.get('local_topology',False),contract.get('mv_structure',False),protection):
                raise ValueError('Experience action replay failed')
            a=assessment(before,spec,json.loads((root/row['parent']/'simulation.json').read_text()))
            b=assessment(after,spec,json.loads((root/row['candidate']/'simulation.json').read_text()))
            if bool(acceptable_step(a,b))!=row['selected']:raise ValueError('Experience selection inconsistent with measurements')
            events.append(dict(kind=row['action']['kind'],improved=row['selected'],before=a['score'],after=b['score'],final_accepted=report['accepted']))
            if row['selected']:current_id=row['candidate']
        if not events:return 0
        files={str(p.relative_to(root)):file_digest(p) for p in sorted(root.rglob('*.json')) if p.name!='memory_status.json'}
        payload=dict(root=str(root),files=files,events=events,verification='action replay and saved solver measurement consistency; not an independent solver rerun')
        key=digest(dict(root=str(root),result=files['result.json']))
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT OR IGNORE INTO episodes VALUES (?,?,?,NULL)',(key,signature(spec),json.dumps(payload)))
        return len(events)

    def retrieve(self,spec,limit=6):
        with sqlite3.connect(self.path) as db:
            rows=db.execute('SELECT id,payload FROM episodes WHERE signature=? AND invalid_reason IS NULL ORDER BY id',(signature(spec),)).fetchall()
        groups=defaultdict(lambda:dict(trials=0,improved=0,rejected=0,final_accepted=0,evidence=[]))
        for key,raw in rows:
            item=json.loads(raw);root=Path(item['root'])
            try:valid=all(file_digest(root/p)==h for p,h in item['files'].items())
            except OSError:valid=False
            if not valid:continue
            for event in item['events']:
                group=groups[event['kind']];group['trials']+=1
                group['improved']+=int(event['improved']);group['rejected']+=int(not event['improved'])
                group['final_accepted']+=int(event['final_accepted'])
                if key not in group['evidence'] and len(group['evidence'])<3:group['evidence'].append(key)
        return [dict(kind=kind,**v,smoothed_improvement_rate=(v['improved']+1)/(v['trials']+2)) for kind,v in sorted(groups.items())][:limit]
