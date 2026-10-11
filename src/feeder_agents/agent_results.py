"""Typed, verified artifact access for the conversational tool boundary."""
import json
import re
from pathlib import Path

FAMILIES = {'experiment': 'experiments', 'electrical_response': 'response_designs',
    'research_task': 'research_tasks', 'transmission': 'transmission_experiments',
    'hierarchical': 'hierarchical_experiments', 'hierarchical_inverse': 'hierarchical_inverse_designs'}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value):
        raise ValueError('Invalid saved-result identifier')
    return value


def read_grid_result(root, result_id, family=None):
    """Resolve a saved ID to its physical artifact family, never an arbitrary path."""
    root = Path(root).resolve(); identifier(result_id)
    if family is not None and family not in FAMILIES:
        raise ValueError('Unknown result family')
    candidates = [(kind, root/folder/result_id) for kind, folder in FAMILIES.items()
        if (family is None or family == kind) and (root/folder/result_id).is_dir()]
    if not candidates:
        raise FileNotFoundError(f'No saved model result for {result_id}. Use the exact ID returned by the design tool; drafts are not physical results.')
    if len(candidates) != 1:
        raise ValueError('Result ID is ambiguous across artifact families; provide family explicitly')
    kind, folder = candidates[0]
    if not folder.resolve().is_relative_to(root):
        raise ValueError('Saved result escaped the project directory')
    if kind == 'experiment':
        from .workflow import read_verified_experiment
        result = read_verified_experiment(folder)
    elif kind in {'electrical_response', 'research_task'}:
        from .response_design import read_response_result
        result = read_response_result(folder)
    elif kind == 'transmission':
        from .transmission import read_transmission_result
        result = read_transmission_result(folder)
    elif kind == 'hierarchical':
        from .hierarchy_workflow import read_hierarchy_result
        result = read_hierarchy_result(folder)
    else:
        from .hierarchy_inverse import read_hierarchy_inverse
        result = read_hierarchy_inverse(folder)
    result = dict(result, artifact_kind=kind, result_id=result_id, directory=str(folder))
    if 'samples' in result:
        result['samples'] = result['samples'][:10]
        result['sample_list_truncated_to'] = 10
    return result


def response_parent(root, parent_id):
    """Load the exact saved response plan; never trust a model-reconstructed plan."""
    root = Path(root); identifier(parent_id)
    result = read_grid_result(root, parent_id)
    if result['artifact_kind'] == 'research_task':
        return result['compiled_plan']['response_plan'], result
    if result['artifact_kind'] == 'electrical_response':
        manifest = json.loads((Path(result['directory'])/'manifest.json').read_text())
        return manifest['plan'], result
    raise ValueError('Permission amendments require a saved response or research-task parent')


def summarize_result(result):
    """Keep verifiable conclusions in context; retain complete arrays in the artifact."""
    keys = ('status', 'artifact_kind', 'result_id', 'directory', 'attempted', 'accepted',
        'failed_or_unaccepted', 'target_met', 'verification_passed', 'protected_contract_preserved',
        'task', 'task_validation', 'diagnosis', 'accepted_steps', 'stop_reason', 'verified_report')
    return {k: result[k] for k in keys if k in result}


def design_output(root, output):
    """Attach a verified handoff only after execution and successful acceptance."""
    keys = ('status', 'directory', 'design_directory', 'outcome', 'questions', 'issues', 'verified_report')
    reduced = {k: output[k] for k in keys if k in output}
    if output.get('status') in {'draft', 'ready'}:
        reduced['plan'] = output.get('plan')
        return reduced
    physical = output.get('outcome', {})
    directory = physical.get('directory')
    if not directory or output.get('status') not in {'completed', 'target_not_met', 'task_not_satisfied', 'completed_unaccepted'}:
        return reduced
    resolved = Path(directory).resolve()
    if not resolved.is_relative_to(Path(root).resolve()):
        raise ValueError('Design outcome escaped the project directory')
    kinds = [kind for kind, folder in FAMILIES.items()
        if resolved.parent == Path(root).resolve()/folder]
    if not kinds:
        # Older study/inverse workflows retain their existing report and normal
        # conversational handoff until a dedicated verified reader is available.
        return reduced
    result = read_grid_result(root, resolved.name, kinds[0])
    accepted = (result.get('accepted', 0) == result.get('attempted', -1) > 0)
    if 'target_met' in result:
        accepted = bool(result['target_met'] and result.get('verification_passed')
            and result.get('protected_contract_preserved', True))
    ready = output.get('status') == 'completed' and accepted
    reduced.update(artifact_kind=result['artifact_kind'], result_id=result['result_id'],
        delivery=dict(ready=ready, directory=result['directory'], result_id=result['result_id'],
            artifact_kind=result['artifact_kind'], verified_report=output.get('verified_report', result.get('verified_report', ''))))
    if not ready:
        reduced['next_step'] = 'Report unmet requirements; modify only within the original permission contract.'
        if result.get('target_met') is False and result['artifact_kind'] in {'research_task', 'electrical_response'}:
            plan = result.get('compiled_plan', {}).get('response_plan')
            if plan is None:
                plan = json.loads((resolved/'manifest.json').read_text()).get('plan', {})
            if plan.get('search', {}).get('allowed_actions') == []:
                reduced['delivery']['terminal_reason'] = 'no_authorized_actions'
    return reduced
