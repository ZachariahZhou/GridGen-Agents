import math
import json
from typing import Literal
from pydantic import Field,model_validator
from ..schemas import StrictModel
from ..hierarchy import HierarchicalSpec

Id = str


class Requirement(StrictModel):
    id: str=Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$')
    field: Literal['n_buses','users','transformers','lv_branches','total_kw','pv_ratio','mv_voltage_kv','lv_voltage_kv','lv_installations','region_installation']
    operator: Literal['eq','ge','le','contains','excludes']='eq'
    value: float | str | list[str]
    region_name: str | None=None
    tolerance: float=Field(default=1e-6,ge=0,le=.1)
    evidence: str=Field(min_length=1,max_length=1000)

    @model_validator(mode='after')
    def types(self):
        if self.field=='region_installation':
            if not self.region_name or not isinstance(self.value,str) or self.operator!='eq':raise ValueError('Region installation requires region_name and string equality')
        elif self.region_name is not None:raise ValueError('region_name only applies to region_installation')
        if self.field in ('lv_installations','region_installation'):
            values=self.value if isinstance(self.value,list) else [self.value]
            if not values or any(v not in ('aerial_bundle','buried_direct','buried_duct','legacy_equivalent') for v in values):raise ValueError('Invalid installation target')
            if self.operator not in ('eq','contains','excludes'):raise ValueError('Installation checks use equality, contains or excludes')
        elif not isinstance(self.value,(int,float)) or not math.isfinite(self.value) or self.operator in ('contains','excludes'):
            raise ValueError('Numeric requirement requires finite numeric value and eq/ge/le')
        return self


class BenchmarkCase(StrictModel):
    id: str=Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$')
    group_id: str=Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$',description='Paraphrases share a group; splitting by group is external to this suite')
    request: str=Field(min_length=1,max_length=12000)
    expected: Literal['generate','reject','abstain']='generate'
    requirements: list[Requirement]=Field(default_factory=list,max_length=40)
    oracle_spec: HierarchicalSpec | None=None
    notes: str=''
    response_rubric: list[str]=Field(default_factory=list,max_length=12,description='Private manual-review criteria, not a keyword judge')
    stratum: Literal['scalar','context','regional','unsupported','boundary','negation','conflict']='scalar'

    @model_validator(mode='after')
    def check_key(self):
        if self.expected=='generate' and not self.requirements:raise ValueError('Generation cases require independent requirements')
        if self.expected!='generate' and (self.requirements or self.oracle_spec is not None):raise ValueError('Rejection cases have no generated-model key')
        ids=[r.id for r in self.requirements]
        if len(ids)!=len(set(ids)):raise ValueError('Duplicate requirement ID')
        if any(r.evidence not in self.request for r in self.requirements):raise ValueError('Requirement evidence not in request')
        if self.oracle_spec is not None and self.oracle_spec.count!=1:raise ValueError('Benchmark oracle must generate one sample')
        return self


class BenchmarkSuite(StrictModel):
    id: str=Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$')
    split: Literal['development','heldout']='development'
    seeds: list[int]=Field(default_factory=lambda:[42],min_length=1,max_length=100)
    adapters: list[Literal['fixed_template','llm_extract','llm_plan','full_agent','oracle']]=Field(min_length=1,max_length=5)
    fixed_spec: HierarchicalSpec=Field(default_factory=HierarchicalSpec)
    cases: list[BenchmarkCase]=Field(min_length=1,max_length=1000)

    @model_validator(mode='after')
    def unique(self):
        for values in (self.seeds,self.adapters,[c.id for c in self.cases]):
            if len(values)!=len(set(values)):raise ValueError('Duplicate case, adapter or seed')
        if any(isinstance(s,bool) or not 0<=s<=2**32-1 for s in self.seeds):raise ValueError('Invalid seed')
        if self.fixed_spec.count!=1:raise ValueError('Fixed template must generate one sample')
        if 'oracle' in self.adapters and any(c.expected=='generate' and c.oracle_spec is None for c in self.cases):raise ValueError('Oracle adapter requires oracle_spec for every generation case')
        groups={}
        for case in self.cases:
            # Quotes/IDs may differ across paraphrases; semantic answer keys must agree.
            key=(case.expected,case.stratum,tuple(sorted(json.dumps(r.model_dump(exclude={'id','evidence'}),sort_keys=True) for r in case.requirements)))
            if case.group_id in groups and groups[case.group_id]!=key:
                raise ValueError('Paraphrase groups must have equivalent requirements and strata')
            groups[case.group_id]=key
        return self
