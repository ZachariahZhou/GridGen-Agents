"""Transactional, scope-checked proposal edits; never an acceptance shortcut."""
import copy
from typing import Literal

from pydantic import Field, JsonValue

from .artifacts import digest
from .schemas import StrictModel


class ProposalEdit(StrictModel):
    path: str = Field(min_length=1, max_length=160, pattern=r'^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$')
    operation: Literal['set', 'remove'] = 'set'
    value: JsonValue = None


def _assign(candidate, edit):
    node=candidate
    *parents,leaf=edit['path'].split('.')
    for part in parents:
        if part not in node:
            if edit.get('operation','set')=='remove':raise ValueError('Patch remove path is absent: '+edit['path'])
            node[part]={}
        if not isinstance(node[part],dict):raise ValueError('Patch cannot traverse a non-object: '+edit['path'])
        node=node[part]
    if edit.get('operation','set')=='remove':
        if leaf not in node:raise ValueError('Patch remove path is absent: '+edit['path'])
        del node[leaf]
    else:node[leaf]=copy.deepcopy(edit.get('value'))


def derive_patch_values(candidate, paths):
    """Recompute only existing schema dependencies, preserving source-bound totals."""
    from .requirement_contract import canonical
    bound={canonical(r.get('target_field') or '') for r in (candidate.get('ledger') or {}).get('requirements',[])
           if r.get('priority')=='hard'}
    family=candidate.get('family')
    prefix={'single_voltage':'single_voltage_spec','hierarchical':'distribution_spec','transmission':'transmission_spec'}.get(family)
    if prefix is None:return
    spec=candidate.get(prefix)
    if not isinstance(spec,dict):return
    leaves={p[len(prefix)+1:] for p in paths if p.startswith(prefix+'.')}
    if family=='hierarchical' and leaves & {'users','transformer_count','lv_branches','mv_buses'} and 'n_buses' not in leaves:
        if 'n_buses' not in bound:spec.pop('n_buses',None)
    if family=='single_voltage' and 'n_buses' in leaves and not bound & {'n_loads','n_loads_min','n_loads_max'}:
        for k in ('n_loads_min','n_loads_max'):
            if k not in leaves:spec.pop(k,None)
    if family=='transmission':
        if leaves & {'topology','n_buses','voltage_layers'} and 'extra_edges' not in leaves and 'extra_edges' not in bound:spec.pop('extra_edges',None)
        if 'voltage_layers' in leaves and 'voltage_kv' not in leaves and 'voltage_kv' not in bound:spec.pop('voltage_kv',None)
        if 'n_generators' in leaves and 'generator_types' not in leaves and len(set(spec.get('generator_types') or ['generic']))==1:
            spec['generator_types']=(spec.get('generator_types') or ['generic'])[:1]


def apply_scoped_patch(base, edits, action, base_candidate_hash):
    from .planning_diagnostics import normalize_candidate, check_adjustment
    if digest(base)!=base_candidate_hash:raise ValueError('Patch base hash does not match the authorized candidate')
    if action.get('id') not in ('align_spec','repair_representation'):raise ValueError('This action requires source interpretation, not a scoped spec patch')
    parsed=[ProposalEdit.model_validate(e).model_dump() for e in edits]
    if not parsed:raise ValueError('Patch contains no edits')
    paths=[e['path'] for e in parsed]
    for i,path in enumerate(paths):
        if any(path==other or path.startswith(other+'.') or other.startswith(path+'.') for other in paths[:i]):
            raise ValueError('Patch paths overlap or duplicate')
        if path.startswith('ledger'):raise ValueError('Scoped specification patch cannot edit the requirement ledger')
        if not any(path==allowed or path.startswith(allowed+'.') for allowed in action.get('paths',[])):
            raise ValueError('Patch path outside allowed scope: '+path)
    candidate=normalize_candidate(base)
    for edit in parsed:_assign(candidate,edit)
    derive_patch_values(candidate,paths)
    # This verifies all unchanged values, protected settings and schema dependencies.
    # Full compile/source/semantic checks still follow at the controller boundary.
    check_adjustment(base,candidate,action)
    return candidate


def patched_supplied_fields(supplied, edits):
    """Keep input/default provenance when editing a normalized single-voltage spec."""
    result={'single_voltage_spec':copy.deepcopy(supplied)}
    for raw in edits:
        edit=ProposalEdit.model_validate(raw).model_dump()
        if not edit['path'].startswith('single_voltage_spec.'):continue
        if edit['operation']=='remove':
            try:_assign(result,edit)
            except ValueError:pass  # A schema default need not have been supplied.
        else:_assign(result,edit)
    return result['single_voltage_spec']
