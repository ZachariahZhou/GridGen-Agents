"""Executable contracts for three static, model-generating research tasks."""
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .schemas import ExperimentSpec, StrictModel
from .transmission import TransmissionSpec
from .response_schema import ResponseSearch


class ResearchTaskPlan(StrictModel):
    task: Literal['voltage_control','static_pv_impact','transmission_transfer']
    research_question: str = Field(min_length=1,max_length=4000)
    base_spec: Annotated[ExperimentSpec | TransmissionSpec,Field(discriminator='network_kind')]
    response_reference: Literal['baseline_ratio','absolute'] = 'baseline_ratio'
    response_lower: float | None = Field(default=None,ge=0)
    response_upper: float | None = Field(default=None,ge=0)
    injection_buses: list[str] = Field(default_factory=list,max_length=100)
    withdrawal_buses: list[str] = Field(default_factory=list,max_length=100)
    monitor_buses: list[str] = Field(default_factory=list,max_length=100)
    monitor_branches: list[int] = Field(default_factory=list,max_length=700)
    phases: list[Literal[1,2,3]] | None = None
    probe_step: float | None = Field(default=None,gt=0,le=10)
    support_mvar: float = Field(default=.01,gt=0,le=100)
    transfer_mw: float = Field(default=1.,gt=0,le=10000)
    preserve_geometry: bool = False
    preserve_equipment: bool = False
    search: ResponseSearch | None = None
    assumptions: list[str] = Field(default_factory=list,max_length=30)

    @model_validator(mode='after')
    def compatible(self):
        transmission=self.task=='transmission_transfer'
        if transmission!=(self.base_spec.network_kind=='transmission'):
            raise ValueError('transmission_transfer requires transmission; voltage/PV tasks require distribution')
        if self.base_spec.count!=1:raise ValueError('One fixed original model per task; repeat plans for ensembles')
        if not transmission and self.base_spec.mode!='normal':raise ValueError('Task reference must use normal mode')
        if self.task=='static_pv_impact' and self.base_spec.pv_ratio<=0:raise ValueError('Static PV impact requires positive PV in the delivered model')
        if self.response_lower is not None and self.response_upper is not None and self.response_lower>self.response_upper:
            raise ValueError('Conflicting response interval')
        if self.response_reference=='absolute' and self.response_lower is None and self.response_upper is None:
            raise ValueError('Absolute response needs an explicit interval')
        if transmission and (self.phases or self.monitor_buses):raise ValueError('Transmission observes branches, not phases or bus voltages')
        if not transmission and (self.withdrawal_buses or self.monitor_branches):raise ValueError('Distribution balances at the source and observes bus phases')
        if transmission and bool(self.injection_buses)!=bool(self.withdrawal_buses):raise ValueError('Provide both transfer groups or let the task select both')
        if self.search:
            actions=set(self.search.allowed_actions)
            if self.preserve_geometry and actions&{'scale_layout','scale_subtree'}:raise ValueError('Search conflicts with fixed geometry')
            if self.preserve_equipment and actions&{'replace_conductor','parallel_line'}:raise ValueError('Search conflicts with fixed equipment')
        return self
