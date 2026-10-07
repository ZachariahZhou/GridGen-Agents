"""Resolve request-bound references; do not infer or change requirement meaning."""
import copy
import re

from .artifacts import digest


def source_catalog(request):
    """Full clauses and comma atoms, with Unicode offsets into immutable source."""
    rows=[]
    fingerprint=digest(request)[:16]
    for segment_id,match in enumerate(m for m in re.finditer(r'[^。；;\n]+',request) if m.group().strip()):
        start=match.start()+len(match.group())-len(match.group().lstrip())
        end=match.end()-len(match.group())+len(match.group().rstrip())
        def add(label,a,b):
            rows.append(dict(ref=f'@source:{fingerprint}:{label}',segment_id=segment_id,
                             start=a,end=b,text=request[a:b]))
        add(f's{segment_id}',start,end)
        for index,atom in enumerate(re.finditer(r'[^，,]+',request[start:end])):
            text=atom.group()
            a=start+atom.start()+len(text)-len(text.lstrip())
            b=start+atom.end()-len(text)+len(text.rstrip())
            if a<b and (a,b)!=(start,end):add(f's{segment_id}a{index}',a,b)
    return rows


def _resolve(value,request,catalog,path,bindings,strict=True):
    # A literal occurrence in the source is data, even if it resembles a token.
    if not isinstance(value,str) or value in request or not value.startswith('@source:'):return value
    row=catalog.get(value)
    if row is None:
        if not strict:return value
        from .planning_diagnostics import PlanningValidationError
        raise PlanningValidationError('Unknown or stale source reference: '+value,'source_evidence',
            [dict(field=path,supplied_quote=value,observation='Select a reference from this request source_catalog')])
    bindings.append(dict(path=path,**row))
    return row['text']


def _ledger_sources(payload,request,catalog,bindings,strict=True):
    items=payload.get('requirements')
    if not isinstance(items,list):return
    for index,item in enumerate(items):
        if not isinstance(item,dict):continue
        before=item.get('evidence')
        after=_resolve(before,request,catalog,f'ledger.requirements.{index}.evidence',bindings,strict)
        if after!=before:
            item['evidence']=after
            item['segment_ids']=[catalog[before]['segment_id']]
        elif not item.get('segment_ids') and isinstance(after,str) and after and after in request:
            from .requirements import request_segments
            item['segment_ids']=[i for i,segment in enumerate(request_segments(request)) if after in segment or segment in after]


def bind_ledger_sources(ledger,request):
    from .requirements import RequirementLedger
    raw=ledger.model_dump();bindings=[]
    _ledger_sources(raw,request,{r['ref']:r for r in source_catalog(request)},bindings)
    return RequirementLedger.model_validate(raw),bindings


def bind_payload_sources(payload,request,strict=True):
    """A comparison view for raw, potentially schema-invalid candidates.

    Callers retain the original candidate and hash; no scope is granted here.
    Unknown old references may remain only in an authorization comparison view.
    """
    raw=copy.deepcopy(payload);bindings=[]
    catalog={r['ref']:r for r in source_catalog(request)}
    if isinstance(raw.get('ledger'),dict):_ledger_sources(raw['ledger'],request,catalog,bindings,strict)
    spec=raw.get('distribution_spec') or {}
    if not isinstance(spec,dict):return raw,bindings
    decisions=[('distribution_spec.lv_installation_decision',spec.get('lv_installation_decision'))]
    regions=spec.get('lv_regions')
    if isinstance(regions,list):
        decisions += [(f'distribution_spec.lv_regions.{i}.decision',r.get('decision'))
                      for i,r in enumerate(regions) if isinstance(r,dict)]
    for path,decision in decisions:
        if not isinstance(decision,dict):continue
        factors=decision.get('factors')
        if not isinstance(factors,list):continue
        for index,factor in enumerate(factors):
            if not isinstance(factor,dict):continue
            factor['evidence']=_resolve(factor.get('evidence'),request,catalog,
                                       f'{path}.factors.{index}.evidence',bindings,strict)
    return raw,bindings


def bind_proposal_sources(proposal,request):
    """Resolve only evidence fields, atomically, before scope/source checks."""
    raw,bindings=bind_payload_sources(proposal.model_dump(),request)
    if not bindings:return proposal
    result=type(proposal).model_validate(raw)
    result.__pydantic_private__=copy.deepcopy(proposal.__pydantic_private__)
    result._source_bindings.extend(bindings)
    return result
