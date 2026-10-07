import math
from typing import Literal
from pydantic import Field,model_validator
from ..schemas import StrictModel
from ..hierarchy import HierarchicalSpec
from ..transmission import TransmissionSpec


class Requirement(StrictModel):
    field: Literal['n_buses','users','transformers','total_kw','pv_ratio','mv_voltage_kv','lv_voltage_kv','scene','installations','n_generators','total_mw','voltage_levels','generator_types','min_voltage_pu','max_voltage_pu','max_branch_loading','phase_model']
    op: Literal['eq','ge','le','contains','excludes']='eq'
    value: float | str | list[str]
    tolerance: float=Field(default=1e-6,ge=0,le=.1)
    evidence: str=Field(min_length=1)

    @model_validator(mode='after')
    def valid(self):
        if isinstance(self.value,float) and not math.isfinite(self.value):raise ValueError('Finite requirement needed')
        if self.op in ('ge','le') and not isinstance(self.value,(int,float)):raise ValueError('Numeric bound required')
        return self


class Task(StrictModel):
    id: str=Field(pattern=r'^[a-zA-Z0-9_-]{1,60}$')
    group: str=Field(pattern=r'^[a-zA-Z0-9_-]{1,60}$')
    domain: Literal['distribution','transmission']
    request: str=Field(min_length=1,max_length=12000)
    expected: Literal['generate','clarify','reject']='generate'
    reference_spec: dict | None=None
    requirements: list[Requirement]=Field(default_factory=list)
    response_rubric: list[str]=Field(default_factory=list)
    fault: Literal['none','undersized_transformer','undercompensated']='none'

    @model_validator(mode='after')
    def valid(self):
        if self.expected=='generate':
            if not self.requirements or self.reference_spec is None:raise ValueError('Generation requires independent requirements and reference specification')
            spec=(HierarchicalSpec if self.domain=='distribution' else TransmissionSpec).model_validate(self.reference_spec)
            if spec.count!=1:raise ValueError('One sample per trial')
        elif self.reference_spec is not None or self.requirements or not self.response_rubric:raise ValueError('Boundary tasks require rubric, no generated-model answer')
        if any(r.evidence not in self.request for r in self.requirements):raise ValueError('Evidence must be verbatim')
        if self.fault!='none' and (self.expected!='generate' or (self.fault=='undercompensated')!=(self.domain=='transmission')):raise ValueError('Fault/domain mismatch')
        return self


class Protocol(StrictModel):
    id: str=Field(pattern=r'^[a-zA-Z0-9_-]{1,60}$')
    split: Literal['development','candidate','heldout']='development'
    execution: Literal['component','end_to_end']='component'
    recovery_rounds: int=Field(default=2,ge=0,le=2)
    seeds: list[int]=Field(default_factory=lambda:[41,42],min_length=1)
    tracks: list[Literal['language','structured']]=Field(default_factory=lambda:['structured'],min_length=1)
    methods: list[Literal['template','one_shot','fixed_repair','agent','agent_memory','adaptive']]=Field(default_factory=lambda:['one_shot','fixed_repair'],min_length=1)
    rounds: int=Field(default=4,ge=0,le=8)
    memory_snapshots: dict[str,str]=Field(default_factory=dict)
    tasks: list[Task]=Field(min_length=1)

    @model_validator(mode='after')
    def valid(self):
        if self.execution=='end_to_end':
            if self.tracks!=['language']:raise ValueError('End-to-end execution requires language input only')
            if any(m not in ('one_shot','fixed_repair','agent','adaptive') for m in self.methods):
                raise ValueError('End-to-end methods support one_shot/fixed_repair/agent/adaptive; memory snapshots require a separate study')
        for values in (self.seeds,self.tracks,self.methods,[t.id for t in self.tasks]):
            if len(values)!=len(set(values)):raise ValueError('Duplicate protocol entries')
        if any(not 0<=seed<=2**32-1 for seed in self.seeds):raise ValueError('Invalid seed')
        if any(t.fault!='none' for t in self.tasks) and 'language' in self.tracks:raise ValueError('Controlled faults belong to the structured diagnostic track')
        if 'agent_memory' in self.methods and any(t.domain not in self.memory_snapshots for t in self.tasks):raise ValueError('Memory method requires an explicit frozen snapshot for each domain')
        groups={}
        from ..artifacts import digest
        for t in self.tasks:
            key=digest(dict(domain=t.domain,expected=t.expected,requirements=sorted([r.model_dump(exclude={'evidence'}) for r in t.requirements],key=lambda r:r['field']),fault=t.fault))
            if t.group in groups and groups[t.group]!=key:raise ValueError('Paraphrase group answer keys differ')
            groups[t.group]=key
        return self
