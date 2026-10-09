"""Conservative source checks for task quantities and effective edit permissions."""
import math
import re

from .source_intent import numeric_source_atoms,numeric_constraints,explicit_pv_values

NUMBER=r'\d+(?:\.\d+)?'
AMOUNT=f'(?P<value>{NUMBER})(?:\\s*(?:\u5230|\u81f3|to|and|[-~～–])\\s*(?P<upper>{NUMBER}))?'
GAP=r'[^\d。；;，,\n]{0,24}?'


def task_source_issues(request,plan):
    from .research_tasks import effective_search
    from .response_language import _layout_permission,_local_permission_issues
    search=effective_search(plan);issues=[];spec=plan.base_spec
    bound=_layout_permission(request)
    if bound is not None and 'scale_layout' in search.allowed_actions and search.max_layout_scale_change>bound+1e-9:
        issues.append('Effective layout permission exceeds the explicit input limit.')
    if re.search('\u53ea\u5141\u8bb8(?:\u66f4\u6362|\u66ff\u6362)\u5bfc\u7ebf|\u4ec5(?:\u66f4\u6362|\u66ff\u6362)\u5bfc\u7ebf|conductor[- ]only|only (?:replace|change) conductors',request,re.I):
        if set(search.allowed_actions)-{'replace_conductor'}:issues.append('Conductor-only input forbids additional default actions.')
    if re.search('\u4ec5\u6d4b\u91cf|\u53ea\u6d4b\u91cf|\u4e0d(?:\u8fdb\u884c)?\u8c03\u6574|measure(?:ment)? only|no (?:changes|edits)',request,re.I) and search.allowed_actions and search.max_rounds:
        issues.append('Measurement-only input forbids design edits.')
    issues.extend(_local_permission_issues(request,search))
    scalar_values={'support_mvar':plan.support_mvar,'transfer_mw':plan.transfer_mw,
        'n_buses':spec.n_buses,'total_power':spec.total_mw if spec.network_kind=='transmission' else (spec.total_kw_min,spec.total_kw_max)}
    def check(field,expected,op='eq'):
        actual=scalar_values[field];values=actual if isinstance(actual,tuple) else (actual,)
        okay=all(v is not None and (math.isclose(v,expected,rel_tol=1e-8,abs_tol=1e-8) if op=='eq' else v>=expected-1e-8 if op=='ge' else v<=expected+1e-8) for v in values)
        if not okay:issues.append(f'Explicit {field} {op} {expected} differs from the plan: {actual}.')
    patterns=[
        ('support_mvar','(?:\u65e0\u529f(?:\u652f\u6491|\u652f\u6301|\u6ce8\u5165|\u8c03\u8282)(?:\u9884\u7b97|\u5bb9\u91cf)?|(?:reactive(?:[- ]power)?|Q)[ -](?:support|injection)(?: budget)?)'+GAP+AMOUNT+'\\s*(?P<unit>MVAr|kvar|var|\u5146\u4e4f|\u5343\u4e4f|\u4e4f)'),
        ('transfer_mw','(?:\u529f\u7387\u8f6c\u79fb|\u6709\u529f\u8f6c\u79fb|\u8f6c\u79fb\u529f\u7387|transfer(?: magnitude| budget| power)?)'+GAP+AMOUNT+'\\s*(?P<unit>MW|kW|W|\u5146\u74e6|\u5343\u74e6|\u74e6)(?![A-Za-z])'),
        ('total_power','(?:\u603b(?:\u6709\u529f)?(?:\u8d1f\u8377|\u529f\u7387)|total\\s+(?:active\\s+)?(?:load|demand|power))'+GAP+AMOUNT+'\\s*(?P<unit>MW|kW|W|\u5146\u74e6|\u5343\u74e6|\u74e6)(?![A-Za-z])')]
    response_mentions=[]
    for atom in numeric_source_atoms(request):
        text=re.sub('^(?:\u4f46|\u4f46\u662f|but)\\s*','',atom['text'],flags=re.I)
        for value in explicit_pv_values(dict(atom,text=text)):
            if spec.network_kind!='distribution' or not math.isclose(spec.pv_ratio,value,abs_tol=1e-9):issues.append('Explicit PV directive differs from the model.')
        if atom['scope'] not in ('current','bounded'):continue
        for match in re.finditer(AMOUNT+'\\s*(?:buses\\b|nodes\\b|\u4e2a\u8282\u70b9|\u4e2a\u6bcd\u7ebf)',text,re.I):
            for op,value in numeric_constraints(dict(atom,text=text),match):check('n_buses',value,op)
        for field,pattern in patterns:
            for match in re.finditer(pattern,text,re.I):
                unit=match['unit'].lower()
                factor={'mvar':1.,'kvar':.001,'var':.000001,'\u5146\u4e4f':1.,'\u5343\u4e4f':.001,'\u4e4f':.000001}.get(unit) if field=='support_mvar' else {'mw':1.,'kw':.001,'w':.000001,'\u5146\u74e6':1.,'\u5343\u74e6':.001,'\u74e6':.000001}[unit]
                if field=='total_power' and spec.network_kind=='distribution':factor*=1000
                for op,value in numeric_constraints(dict(atom,text=text),match):check(field,value*factor,op)
        for match in re.finditer(r'(?<![\d.])('+NUMBER+')\\s*(?:kV|\u5343\u4f0f)(?![A-Za-z])',text,re.I):
            levels={v.kv for v in spec.voltage_layers} if spec.network_kind=='transmission' and spec.voltage_layers else {spec.voltage_kv}
            if float(match[1]) not in levels:issues.append('Explicit voltage level is absent from the compiled model.')
        for match in re.finditer('(?:\u5149\u4f0f(?:\u6e17\u900f\u7387|\u6bd4\u4f8b|\u5360\u6bd4)|PV (?:penetration|ratio))'+GAP+AMOUNT+r'\s*[%％]',text,re.I):
            actual=getattr(spec,'pv_ratio',None)
            for op,value in numeric_constraints(dict(atom,text=text),match):
                if actual is None or not (math.isclose(actual,value/100,abs_tol=1e-9) if op=='eq' else actual>=value/100 if op=='ge' else actual<=value/100):issues.append('Explicit PV percentage differs from the model.')
        ratio='(?:\u54cd\u5e94(?:\u500d\u7387|\u6bd4\u503c)|response ratio|\u539f\u59cb\u57fa\u51c6(?:\u54cd\u5e94)?\u7684?|original baseline(?: response)?(?: ratio)?)'+GAP+AMOUNT
        for match in re.finditer(ratio,text,re.I):
            response_mentions.extend(('baseline_ratio',op,value) for op,value in numeric_constraints(dict(atom,text=text),match))
        absolute='(?:\u7075\u654f\u5ea6|sensitivity|\u54cd\u5e94|response)'+GAP+AMOUNT+r'\s*(?P<unit>pu/MVAr|pu/MW|MW/MW)'
        for match in re.finditer(absolute,text,re.I):
            unit={'voltage_control':'pu/mvar','static_pv_impact':'pu/mw','transmission_transfer':'mw/mw'}[plan.task]
            if match['unit'].lower()!=unit:issues.append('Response unit does not match the task measurement.')
            response_mentions.extend(('absolute',op,value) for op,value in numeric_constraints(dict(atom,text=text),match))
    custom=plan.response_lower is not None or plan.response_upper is not None
    if custom and not response_mentions:issues.append('Custom response interval has no recognized numerical source; clarify its definition instead of inventing it.')
    if response_mentions:
        references={reference for reference,_,_ in response_mentions}
        lower=[value for _,op,value in response_mentions if op in ('eq','ge')]
        upper=[value for _,op,value in response_mentions if op in ('eq','le')]
        lo=max(lower) if lower else None;hi=min(upper) if upper else None
        if len(references)>1 or (lo is not None and hi is not None and lo>hi):
            issues.append('Source response constraints conflict; no interval may be silently substituted.')
        def same(actual,expected):
            return actual is expected if actual is None or expected is None else math.isclose(actual,expected,rel_tol=1e-8,abs_tol=1e-10)
        if references!={plan.response_reference} or not same(plan.response_lower,lo) or not same(plan.response_upper,hi):
            issues.append('Explicit response interval differs from the compiled task.')
    if re.search('\u9ad8\u7075\u654f\u5ea6|\u4f4e\u7075\u654f\u5ea6|\u9ad8\u654f\u611f\u5ea6|\u4f4e\u654f\u611f\u5ea6|(?:high|low)[ -]sensitivity',request,re.I) and not response_mentions:
        issues.append('Qualitative response level has no numerical or calibrated reference definition.')
    return issues
