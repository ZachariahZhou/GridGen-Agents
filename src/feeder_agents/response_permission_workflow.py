"""Search a declared sequence of permission plans; stop at first verified success."""
import copy
import re
from pathlib import Path

from .artifacts import atomic_json,digest
from .response_permissions import prepare_permission_revision


def prepare_permission_ladder(plan,grants,*,task_context=None):
    if not isinstance(grants,list) or not 1<=len(grants)<=3:
        raise ValueError('Provide one to three explicitly authorized permission stages')
    stages=[prepare_permission_revision(plan,grant) for grant in grants]
    draft=dict(stages[0],authorized_stages=stages,
        policy='Restart each declared stage from the same original construction and ports; stop at first verified success. Count all attempted stages.')
    if task_context is not None:
        draft.update(task_context=copy.deepcopy(task_context),task_context_hash=digest(task_context))
    return draft


def _baseline_identity(result):
    from .schemas import Feeder
    from .response_search import physical_fingerprint
    feeder=Feeder.model_validate_json((Path(result['directory'])/'baseline/model/feeder.json').read_text())
    return dict(physical=physical_fingerprint(dict(kind='distribution',feeder=feeder)),
        probes=result['baseline']['probes'])


def run_permission_ladder(plan,grants,workspace,design_id,*,task_context=None):
    from .response_design import run_response_design
    from .response_search import response_priority
    from .solver_accounting import SolverMeter
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',design_id):
        raise ValueError('Invalid response design ID')
    draft=prepare_permission_ladder(plan,grants,task_context=task_context)
    root=Path(workspace).resolve()/'permission_ladders'/design_id
    # Each stage is cached by the ordinary response runner, but an outer
    # sequence uses a fresh ID so its accumulated-work record is unambiguous.
    root.mkdir(parents=True,exist_ok=False)
    atomic_json(root/'manifest.json',draft)
    stages=[];reference=None;winner=None;winner_stage=None;search_calls=total_calls=0
    attempted=None
    try:
        for index,revision in enumerate(draft['authorized_stages'],1):
            stage_id=f'{design_id[:48]}_{digest(design_id)[:8]}_p{index}'
            attempted=dict(stage=index,design_id=stage_id,permissions=revision['permissions'],
                plan=revision['plan'],directory=str(Path(workspace).resolve()/'response_designs'/stage_id),
                status='running',baseline_verified=False)
            meter=SolverMeter()
            try:
                with meter:
                    task_args={}
                    if task_context is not None:
                        # The ordinary runner checks the task's original model
                        # hash before measurement and includes the finite test
                        # in every candidate's acceptance and verification.
                        task_args['task_context']=copy.deepcopy(draft['task_context'])
                    result=run_response_design(revision['plan'],workspace,stage_id,**task_args)
                attempted.update(directory=result['directory'],result_hash=result['result_hash'],
                    target_met=result['target_met'],deficits=result['selected']['deficits'],
                    historical_solver_accounting=result['solver_accounting'],status='returned')
            except Exception:
                attempted['status']='error'
                raise
            finally:
                accounting=meter.snapshot()
                stage_search=accounting['by_phase'].get('search',0)
                search_calls+=stage_search;total_calls+=accounting['total_ac_calls']
                attempted.update(search_ac_calls=stage_search,total_ac_calls=accounting['total_ac_calls'],
                    actual_solver_accounting=accounting)
                stages.append(attempted)
                atomic_json(root/'progress.json',dict(stages=stages,search_ac_calls=search_calls,total_ac_calls=total_calls))
            identity=_baseline_identity(result)
            if reference is None:reference=identity
            elif reference!=identity:raise ValueError('Permission stages changed the physical baseline, resolved ports or response denominator')
            attempted.update(baseline_verified=True,status='verified')
            atomic_json(root/'progress.json',dict(stages=stages,search_ac_calls=search_calls,total_ac_calls=total_calls))
            eligible=result['protected_contract_preserved'] and result['selected']['base_accepted'] and result['selected']['valid']
            if eligible and (winner is None or result['target_met'] or response_priority(result['selected'])<response_priority(winner['selected'])):
                winner=result;winner_stage=index
            if eligible and result['target_met']:break
        success=bool(winner and winner['target_met'])
        output=dict(status='completed' if success else 'target_not_met',target_met=success,
            selected_stage=winner_stage,stages=stages,result=winner,directory=str(root),
            authorized_stages=draft['authorized_stages'],
            solver_accounting=dict(search_ac_calls=search_calls,total_ac_calls=total_calls,
                scope='Actual AC requests over every attempted permission stage, including exceptions; cached historical work is separate. The per-stage cap is not a total ladder cap.'),
            scope='Development policy with explicitly preauthorized stages. No target relaxation, warm start or permission inferred from failure.')
        if task_context is not None:
            output.update(task_context=draft['task_context'],task_context_hash=draft['task_context_hash'])
        atomic_json(root/'result.json',output)
        return output
    except Exception as exc:
        atomic_json(root/'error.json',dict(error=f'{type(exc).__name__}: {exc}',attempted_stages=stages,
            failed_stage=attempted,solver_accounting=dict(search_ac_calls=search_calls,total_ac_calls=total_calls),
            scope='Partial stage files remain available; an exception is not an accepted ladder result.'))
        raise
