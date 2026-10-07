"""Structured Agent installation analysis, separate from electrical sizing."""
from typing import Annotated, Literal
from pydantic import Field, model_validator
from .schemas import StrictModel

Installation = Literal['aerial_bundle','buried_direct','buried_duct']


class InstallationFactor(StrictModel):
    criterion: Literal['density','load_distribution','corridor','existing_assets','research_objective','environment','installation_requirement']
    observation: str=Field(min_length=1,max_length=400)
    origin: Literal['user','assumption']
    evidence: str=Field(default='',max_length=1000,description='Exact request substring or source_catalog @source reference for user facts; empty for assumptions')

    @model_validator(mode='after')
    def origin_evidence(self):
        if self.origin=='user' and not self.evidence:raise ValueError('User installation factor requires evidence')
        if self.origin=='assumption' and self.evidence:raise ValueError('Assumptions must not masquerade as quoted evidence')
        return self


class InstallationAlternative(StrictModel):
    installation: Installation
    reason: str=Field(min_length=1,max_length=400)


class InstallationComparisons(StrictModel):
    """Fixed input slots; canonical decisions still store three named records."""
    aerial_bundle: str=Field(min_length=1,max_length=400)
    buried_direct: str=Field(min_length=1,max_length=400)
    buried_duct: str=Field(min_length=1,max_length=400)


def installation_input_contract():
    return dict(comparisons={key:'Provide the reason for or against this option under the current source and assumptions.'
                             for key in InstallationComparisons.model_fields},
        selected='Choose exactly one of the three keys; user-fixed installation remains binding.',
        evidence='User factors cite source_catalog references or exact quotes; assumptions have no evidence.',
        quantity_constraints=dict(total_buses='mv_buses + transformer_count * (1 + lv_branches) + users',
                                  minimum_users='transformer_count * lv_branches'),
        note='These are input slots, not completed engineering analysis. No selection or rationale is supplied by this scaffold.')


class InstallationDecision(StrictModel):
    selected: Installation
    reason: str=Field(min_length=1,max_length=600)
    factors: list[InstallationFactor]=Field(min_length=1,max_length=8)
    alternatives: Annotated[list[InstallationAlternative],Field(min_length=3,max_length=3)] | InstallationComparisons=Field(description="Prefer a fixed object with aerial_bundle, buried_direct, buried_duct reason strings. Legacy three-record lists also accepted; canonical output is always a list.")
    unknowns: list[str]=Field(default_factory=list,max_length=8)

    @model_validator(mode='before')
    @classmethod
    def compile_comparisons(cls,value):
        if not isinstance(value,dict):return value
        options=value.get('alternatives')
        if isinstance(options,dict) or isinstance(options,InstallationComparisons):
            fixed=InstallationComparisons.model_validate(options)
            return {**value,'alternatives':[dict(installation=k,reason=v) for k,v in fixed.model_dump().items()]}
        # In ordinary prose "alternatives" can mean the two rejected options.
        # Reuse the already supplied selection reason; never invent a third one.
        names=[o.get('installation') for o in options if isinstance(o,dict)] if isinstance(options,list) else []
        selected=value.get('selected');reason=value.get('reason')
        if (len(names)==2 and all(isinstance(n,str) for n in names) and len(set(names))==2
                and isinstance(selected,str) and selected not in names
                and set(names)|{selected}==set(InstallationComparisons.model_fields)
                and isinstance(reason,str) and 1<=len(reason)<=400):
            return {**value,'alternatives':[*options,dict(installation=selected,reason=reason)]}
        return value

    @model_validator(mode='after')
    def all_options(self):
        if {a.installation for a in self.alternatives}!={'aerial_bundle','buried_direct','buried_duct'}:
            raise ValueError('Installation analysis must compare all three distinct alternatives')
        return self


def validate_request_evidence(decision,request):
    decision=InstallationDecision.model_validate(decision)
    for factor in decision.factors:
        if factor.origin=='user' and factor.evidence not in request:
            raise ValueError('Installation factor evidence not found in request')
    return decision


def decision_record(spec):
    if spec.lv_regions:
        return dict(decision_source='regional_analysis',installation_decision=None,regional_decisions=[r.model_dump() for r in spec.lv_regions])
    if spec.lv_installation_decision is not None:
        return dict(decision_source='agent_analysis',installation_decision=spec.lv_installation_decision.model_dump())
    if spec.lv_installation!='auto' or spec.lv_equipment_profile!='auto':
        return dict(decision_source='configured',installation_decision=None)
    return dict(decision_source='offline_fallback',installation_decision=None,
        decision_notice='No Agent analysis supplied: scene-independent aerial baseline for offline reproducibility, not an engineering recommendation.')


def decision_summary(spec):
    if spec.lv_regions:
        return '\n\n'.join(f'Region {r.name} ({r.id}), transformer indices {r.transformer_indices}:\n'+decision_summary(spec.model_copy(update={'lv_regions':[],'lv_installation_decision':r.decision})) for r in spec.lv_regions)
    from .lv_equipment import resolve_installation
    profile,installation=resolve_installation(spec)
    d=spec.lv_installation_decision
    if d:
        facts='; '.join(f'{f.criterion} [{f.origin}]: {f.observation}' for f in d.factors)
        options='; '.join(f'{a.installation}: {a.reason}' for a in d.alternatives)
        return f'LV installation analysis: {installation} ({profile}). {d.reason}\n\nEvidence: {facts}\n\nAlternatives: {options}\n\nUnknown conditions: '+('; '.join(d.unknowns) or 'None listed')
    record=decision_record(spec)
    return f'LV installation: {installation} ({profile}); decision source: {record["decision_source"]}. '+record.get('decision_notice','')


class InstallationRegion(StrictModel):
    id: str=Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,31}$')
    name: str=Field(min_length=1,max_length=80)
    transformer_indices: list[int]=Field(min_length=1,max_length=32,description='1-based transformer indices; complete disjoint partition across all regions')
    decision: InstallationDecision


def validate_regions(spec):
    regions=spec.lv_regions
    if not regions:return
    if spec.lv_equipment_profile=='legacy_epri':raise ValueError('Regions require product-based LV design')
    if spec.lv_installation_decision is not None:raise ValueError('Use regional decisions or a global decision, not both')
    ids=[r.id for r in regions];indices=[i for r in regions for i in r.transformer_indices]
    if len(ids)!=len(set(ids)) or sorted(indices)!=list(range(1,spec.transformer_count+1)):
        raise ValueError('Regions must uniquely partition all transformer indices 1..transformer_count')
    from .lv_equipment import resolve_installation
    for r in regions:
        # Copy with regions cleared to check global explicit constraints without recursion.
        scoped=spec.model_copy(update={'lv_regions':[],'lv_installation_decision':r.decision})
        resolve_installation(scoped)


def region_for_transformer(spec,transformer_id):
    return next((r for r in spec.lv_regions if transformer_id in {f'tx_{i}' for i in r.transformer_indices}),None)


def region_contract_valid(feeder,spec):
    """Regional choices are hard acceptance constraints, also after inverse edits."""
    if not spec.lv_regions:return True
    mapping={f'tx_{i}':r for r in spec.lv_regions for i in r.transformer_indices}
    if {t.id for t in feeder.transformers}!=set(mapping):return False
    buses={b.id:b for b in feeder.buses}
    for tx in feeder.transformers:
        if any(b not in buses or buses[b].region_id!=mapping[tx.id].id for b in (tx.bus1,tx.bus2)):return False
    for bus in feeder.buses:
        if bus.voltage_kv>=1:continue
        region=mapping.get(bus.transformer_id)
        if region is None or bus.region_id!=region.id:return False
    for edge in feeder.lines:
        a=buses.get(edge.bus1);b=buses.get(edge.bus2)
        if a is None or b is None:return False
        if a.voltage_kv>=1:continue
        if a.transformer_id!=b.transformer_id or a.region_id!=b.region_id:return False
        region=mapping.get(a.transformer_id);code=feeder.equipment_catalog.get(edge.conductor,{})
        if region is None or code.get('installation')!=region.decision.selected:return False
        if edge.construction!=('overhead' if region.decision.selected=='aerial_bundle' else 'cable'):return False
    return True
