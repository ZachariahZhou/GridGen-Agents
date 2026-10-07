"""Failure-isolated reads for local UI history; never certify editable summaries."""
import json
from pathlib import Path


def load_json_record(path):
    path=Path(path)
    try:
        value=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value,dict):raise ValueError('JSON root must be an object')
        return value,None
    except (OSError,UnicodeError,ValueError) as exc:
        return None,f'{path}: {type(exc).__name__}: {exc}'


def load_verified_experiment_record(summary_path):
    """Return reconstructed counts and a warning for stale/corrupt saved prose."""
    from .workflow import read_verified_experiment
    summary_path=Path(summary_path)
    raw,raw_error=load_json_record(summary_path)
    try:
        verified=read_verified_experiment(summary_path.parent)
    except Exception as exc:
        return None,f'{summary_path}: verification failed: {type(exc).__name__}: {exc}'
    if raw_error:
        return verified,raw_error+'; verified artifacts were used instead.'
    counts=('attempted','accepted','operational_pass')
    saved_ids=[s.get('sample_id') for s in raw.get('samples',[]) if isinstance(s,dict)] if isinstance(raw.get('samples'),list) else None
    verified_ids=[s['sample_id'] for s in verified['samples']]
    if any(raw.get(key)!=verified[key] for key in counts) or saved_ids!=verified_ids:
        return verified,f'{summary_path}: saved summary differs from verified artifacts; reconstructed results are shown.'
    return verified,None


def load_verified_design_record(result_path,workspace):
    """Trust executed design text/views only after reading its saved model evidence."""
    from .artifacts import digest
    result_path=Path(result_path)
    raw,error=load_json_record(result_path)
    if error:return None,error
    outcome=raw.get('outcome')
    if outcome is None:
        if raw.get('status') in ('draft','planning_failed','needs_clarification','unsupported'):
            return raw,None
        return None,f'{result_path}: 未验证：已执行设计缺少结果目录。'
    brief,error=load_json_record(result_path.parent/'brief.json')
    if error:return None,f'{result_path}: 未验证：设计条件无法读取；{error}'
    brief_hash=brief.get('brief_hash')
    if not isinstance(brief_hash,str) or digest({k:v for k,v in brief.items() if k!='brief_hash'})!=brief_hash:
        return None,f'{result_path}: 未验证：设计条件摘要不匹配。'
    family=brief.get('plan_type')
    folders={'feeder':'experiments','hierarchical':'hierarchical_experiments',
             'transmission':'transmission_experiments','hierarchical_inverse':'hierarchical_inverse_designs'}
    if family not in folders:
        return None,f'{result_path}: 未验证：该设计类型没有可验证结果读取器。'
    if not isinstance(outcome,dict) or not isinstance(outcome.get('directory'),str):
        return None,f'{result_path}: 未验证：结果目录无效。'
    project=result_path.parent.parent.parent.resolve()
    folder=Path(outcome['directory']).resolve()
    design_id=result_path.parent.name
    if not folder.is_relative_to(Path(workspace).resolve()) or folder.parent!=project/folders[family] or not (
            folder.name==design_id or folder.name.startswith(design_id[:45]+'_recovery_')):
        return None,f'{result_path}: 未验证：结果目录与设计记录不符。'
    try:
        if family=='feeder':
            from .workflow import read_verified_experiment
            verified=read_verified_experiment(folder)
        elif family=='hierarchical':
            from .hierarchy_workflow import read_hierarchy_result
            verified=read_hierarchy_result(folder)
        elif family=='transmission':
            from .transmission import read_transmission_result
            verified=read_transmission_result(folder)
        else:
            from .hierarchy_inverse import read_hierarchy_inverse
            verified=read_hierarchy_inverse(folder)
    except Exception as exc:
        return None,f'{result_path}: 未验证：{type(exc).__name__}: {exc}'
    changed=any(key in outcome and outcome[key]!=verified.get(key) for key in
                ('attempted','accepted','operational_pass','target_met','verification_passed'))
    warning=f'{result_path}: 已保存设计摘要与验证结果不符；显示重新核验的结果。' if changed else None
    report=verified['verified_report']
    displayed={**raw,'verified_family':family,'verified_report':report,'outcome':verified}
    ledger_data=brief.get('requirement_ledger')
    if family in ('feeder','hierarchical','transmission') and ledger_data:
        try:
            from .requirement_contract import audit_delivery
            from .requirements import RequirementLedger
            audit_brief=brief
            recovery_dir=result_path.parent/'recovery'
            if raw.get('delivery_recovery') is not None or folder.name!=design_id:
                trace,trace_error=load_json_record(recovery_dir/'recovery.json')
                selected,selected_error=load_json_record(recovery_dir/'selected_brief.json')
                if trace_error or selected_error:
                    raise ValueError('恢复交付的选定条件或轨迹缺失')
                if Path(trace.get('selected_directory','')).resolve()!=folder:
                    raise ValueError('恢复轨迹与模型目录不一致')
                if selected.get('requirement_ledger')!=ledger_data or selected.get('request')!=brief.get('request') or selected.get('plan_type')!=family:
                    raise ValueError('恢复条件与原始需求合同不一致')
                audit_brief=selected
            ledger=RequirementLedger.model_validate(ledger_data)
            acceptance=audit_delivery(ledger,audit_brief,verified)
            displayed['requirement_acceptance']=acceptance
            displayed['outcome']={**verified,'requirement_accepted':acceptance['jointly_accepted']}
            failed=sum(row['failed'] for row in acceptance['samples'])
            pending=sum(row['unverified'] for row in acceptance['samples'])
            report+=f"\n\n独立需求验收：{acceptance['jointly_accepted']}/{acceptance['attempted']}例通过模型验收与全部硬需求；不满足项{failed}，未取得自动验收证据项{pending}。"
            if not acceptance['all_satisfied']:
                displayed['status']='completed_unaccepted'
        except Exception as exc:
            warning=(warning+' ' if warning else '')+f'{result_path}: 需求合同尚未核验：{type(exc).__name__}: {exc}'
            report+='\n\n模型文件已验证；需求合同尚未核验。'
            displayed['status']='completed_unverified'
    else:
        report+='\n\n模型文件已验证；需求合同尚未核验。'
        displayed['status']='completed_unverified' if family in ('feeder','hierarchical','transmission') else displayed.get('status')
    if verified.get('attempted') is not None and verified.get('accepted')!=verified['attempted']:
        displayed['status']='completed_unaccepted'
    displayed['verified_report']=report
    return displayed,warning


def build_verified_feeder_zip(verified):
    """Build a downloadable feeder bundle only from hash-checked saved evidence."""
    import hashlib
    import io
    import re
    import zipfile
    from pathlib import PurePosixPath
    from .artifacts import digest

    root=Path(verified['directory']).resolve()
    manifest_bytes=(root/'manifest.json').read_bytes()
    manifest=json.loads(manifest_bytes)
    recorded=manifest.get('configuration_hash')
    if not isinstance(recorded,str) or digest({k:v for k,v in manifest.items() if k!='configuration_hash'})!=recorded or recorded!=verified['configuration_hash']:
        raise ValueError('Experiment manifest hash mismatch')
    bundle=io.BytesIO()
    with zipfile.ZipFile(bundle,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json',manifest_bytes)
        archive.writestr('summary.json',json.dumps(verified,indent=2,ensure_ascii=False,allow_nan=False))
        archive.writestr('report.md',verified['verified_report'])
        for row in verified['samples']:
            sample_id=row['sample_id']
            if not re.fullmatch(r'sample_[0-9]{5}',sample_id):
                raise ValueError('Invalid verified sample ID')
            artifacts=row.get('artifacts',{})
            if not isinstance(artifacts,dict):
                raise ValueError('Invalid verified artifact manifest')
            for name,expected in sorted(artifacts.items()):
                if not isinstance(name,str) or not isinstance(expected,str):
                    raise ValueError('Invalid verified artifact entry')
                path=PurePosixPath(name)
                if path.is_absolute() or name!=str(path) or '\\' in name or any(part in ('','.','..') for part in name.split('/')):
                    raise ValueError('Unsafe verified artifact path')
                source=(root/sample_id/name).resolve()
                if not source.is_relative_to(root/sample_id) or not source.is_file():
                    raise ValueError('Missing verified artifact: '+sample_id+'/'+name)
                payload=source.read_bytes()
                if hashlib.sha256(payload).hexdigest()!=expected:
                    raise ValueError('Changed verified artifact: '+sample_id+'/'+name)
                archive.writestr(sample_id+'/'+name,payload)
    return bundle.getvalue()
