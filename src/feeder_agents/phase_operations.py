"""Keep scalar and per-phase research powers consistent during model edits."""
from .schemas import PhasePower


def set_phase_pv(load, weights):
    if not load.phase_powers:return
    total=sum(weights[p.phase-1] for p in load.phase_powers)
    for power in load.phase_powers:power.pv_kw=load.pv_kw*weights[power.phase-1]/total


def refresh_phase_evidence(feeder,spec):
    from .phases import _evidence
    _evidence(feeder,spec,sum(len(l.phases)==1 for l in feeder.lines),sum(len(l.phases)==2 for l in feeder.lines))


def rebalance_powers(feeder,spec):
    """Change allocations only; line/bus phase connectivity is frozen."""
    if feeder.phase_mode!='unbalanced':raise ValueError('Phase rebalancing requires an existing unbalanced case')
    weights=spec.phase_design.load_phase_weights
    pv_weights=spec.phase_design.pv_phase_weights or weights
    for load in feeder.loads:
        total=sum(weights[p-1] for p in load.phases)
        load.phase_powers=[PhasePower(phase=p,kw=load.kw*weights[p-1]/total,
            kvar=load.kvar*weights[p-1]/total,pv_kw=0) for p in load.phases]
        set_phase_pv(load,pv_weights)
    refresh_phase_evidence(feeder,spec)
