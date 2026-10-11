"""Conservative source checks for task quantities and effective edit permissions."""
import math
import re

from .source_intent import numeric_source_atoms,numeric_constraints,explicit_pv_values

NUMBER=r'\d+(?:\.\d+)?'
AMOUNT=f'(?P<value>{NUMBER})(?:\\s*(?:\u5230|\u81f3|to|and|[-~～–])\\s*(?P<upper>{NUMBER}))?'
GAP=r'[^\d。；;，,\n]{0,24}?'


def source_sentences(request):
    """Separate assertions without splitting decimal quantities."""
    return re.split(r'(?<!\d)\.|\.(?!\d)|[。；;\n]', request)


def response_constraints(request):
    """Read explicit response bounds in source order, never repair their ordering."""
    constraints=[]
    for sentence in source_sentences(request):
        if not re.search('\u54cd\u5e94|\u7075\u654f\u5ea6|sensitivity|response',sentence,re.I):continue
        if re.search('\\b(?:previous|historical|example)\\b|do not require|\u4e0d\u8981\u6c42',sentence,re.I):continue
        absolute=re.search(r'pu/MVAr|pu/MW|MW/MW',sentence,re.I)
        relative=re.search('\u54cd\u5e94(?:\u500d\u7387|\u6bd4\u503c)|response ratio|\u57fa\u51c6|baseline',sentence,re.I)
        if not absolute and not relative:continue
        # Probe units describe the measured sensitivity; an explicit multiplier
        # of the original baseline still requests a normalized response ratio.
        baseline_multiplier=re.search(r'\btimes\s+(?:(?:the|original)\s+){0,2}baseline\b'
                                      '|(?:\u539f\u59cb)?\u57fa\u51c6[^\u3002\uff1b;\\n]{0,32}\u500d',sentence,re.I)
        reference='absolute' if absolute and not baseline_multiplier else 'baseline_ratio'
        found=[]
        # Each explicitly named end remains that end, even when lower > upper.
        labels='(?P<label>at least|at most|no less than|no more than|lower(?: bound)?|upper(?: bound)?|\u81f3\u5c11|\u81f3\u591a|\u4e0d\u5c11\u4e8e|\u4e0d\u8d85\u8fc7|\u4e0b\u9650|\u4e0a\u9650|>=|<=|\u2265|\u2264)'
        for match in re.finditer(labels+'\\s*(?:of|is|=|:|\u4e3a|\u662f)?\\s*(?P<value>'+NUMBER+r')',sentence,re.I):
            label=match['label'].lower()
            op='ge' if label in ('at least','no less than','lower','lower bound','\u81f3\u5c11','\u4e0d\u5c11\u4e8e','\u4e0b\u9650','>=','≥') else 'le'
            found.append((reference,op,float(match['value'])))
        interval=re.search(r'(?:between\s+)?(?P<value>'+NUMBER+')\\s*(?:\u5230|\u81f3|to|and|[-~\uff5e\u2013])\\s*(?P<upper>'+NUMBER+r')',sentence,re.I)
        if interval:
            found.extend([(reference,'ge',float(interval['value'])),(reference,'le',float(interval['upper']))])
        if not found:
            pattern='(?:\u54cd\u5e94(?:\u500d\u7387|\u6bd4\u503c)?|\u7075\u654f\u5ea6|sensitivity|response(?: ratio)?|original baseline(?: response)?(?: ratio)?)'+GAP+AMOUNT
            for match in re.finditer(pattern,sentence,re.I):
                found.extend((reference,op,value) for op,value in numeric_constraints(dict(text=sentence),match))
        constraints.extend(found)
    return constraints


def response_interval_issues(request):
    """Preflight only explicit contradictions, independent of an LLM candidate."""
    mentions=response_constraints(request)
    for reference in {ref for ref,_,_ in mentions}:
        lower=[value for ref,op,value in mentions if ref==reference and op in ('eq','ge')]
        upper=[value for ref,op,value in mentions if ref==reference and op in ('eq','le')]
        if lower and upper and max(lower)>min(upper):
            return ['Source response constraints conflict: the requested lower bound exceeds the upper bound; no interval may be silently substituted.']
    return []


def research_source_contract(request,network_kind):
    """Supply exact source-backed field values to the compiler, never edit its answer."""
    constraints=[]
    response=[]
    for sentence in source_sentences(request):
        if re.search(r'\b(?:previous|historical|example)\b',sentence,re.I):continue
        if re.search('(?:disable|no|without)\\s+automatic repairs?|(?:\u7981\u6b62|\u7981\u7528)\u81ea\u52a8\u4fee\u590d|\u4e0d(?:\u5141\u8bb8)?\u81ea\u52a8\u4fee\u590d',sentence,re.I):
            path='plan.base_spec.allowed_repairs' if network_kind=='transmission' else 'plan.base_spec.repair_policy.strategy'
            constraints.append(dict(path=path,value=[] if network_kind=='transmission' else 'none',source=sentence.strip()))
        response.extend((reference,op,value,sentence.strip()) for reference,op,value in response_constraints(sentence))
    references={reference for reference,_,_,_ in response}
    if len(references)==1 and not response_interval_issues(request):
        constraints.append(dict(path='plan.response_reference',value=next(iter(references)),source=response[0][3]))
        for field,operators,select in [('response_lower',('eq','ge'),max),('response_upper',('eq','le'),min)]:
            values=[(value,source) for _,op,value,source in response if op in operators]
            if values:
                value=select(value for value,_ in values)
                constraints.append(dict(path='plan.'+field,value=value,source=next(source for v,source in values if v==value)))
            else:
                constraints.append(dict(path='plan.'+field,value=None,source=response[0][3]))
    from .response_language import _exclusive_layout_permission
    grant=_exclusive_layout_permission(request) if network_kind=='distribution' else None
    if grant is not None:
        bound,source=grant
        constraints.extend(dict(path=path,value=value,source=source) for path,value in (
            ('plan.preserve_geometry',False),('plan.search.allowed_actions',['scale_layout']),
            ('plan.search.max_layout_scale_change',bound)))
    return constraints


def construction_source_issues(request,spec):
    """Check explicit construction controls shared by research/response compilers."""
    issues=[]
    for sentence in source_sentences(request):
        if re.search(r'\b(?:previous|historical|example)\b',sentence,re.I):continue
        for match in re.finditer('(?:seed|\u968f\u673a\u79cd\u5b50)\\s*(?:is|=|:|\u4e3a)?\\s*(\\d+)',sentence,re.I):
            if spec.seed!=int(match[1]):issues.append('Explicit physical generation seed differs from the compiled model.')
        for match in re.finditer(r'(?:generate|create|build)\s+(\d+|one|two|three|four|five)\s+(?:[a-z-]+\s+){0,4}(?:feeders?|grids?|networks?)\b',sentence,re.I):
            raw=match[1].lower(); count=int(raw) if raw.isdigit() else {'one':1,'two':2,'three':3,'four':4,'five':5}[raw]
            if spec.count!=count:issues.append('Explicit model count differs; separate task runs are required for multiple models.')
        if re.search('(?:disable|no|without)\\s+automatic repairs?|(?:\u7981\u6b62|\u7981\u7528)\u81ea\u52a8\u4fee\u590d|\u4e0d(?:\u5141\u8bb8)?\u81ea\u52a8\u4fee\u590d',sentence,re.I):
            disabled=not spec.allowed_repairs if spec.network_kind=='transmission' else spec.repair_policy.strategy=='none'
            if not disabled:issues.append('Input disables automatic repairs but the compiled construction does not.')
        if spec.network_kind=='distribution':
            for match in re.finditer(r'PV\s+(?:capacity|penetration|ratio)\s*(?:equal to|is|of|=)?\s*('+NUMBER+r')\s*%',sentence,re.I):
                if not math.isclose(spec.pv_ratio,float(match[1])/100,abs_tol=1e-9):issues.append('Explicit PV percentage differs from the model.')
            for match in re.finditer(r'\b(unbalanced|balanced)\s+three[- ]phase',sentence,re.I):
                if spec.phase_design.mode!=match[1].lower():issues.append('Explicit phase representation differs from the model.')
            for match in re.finditer(r'\b(rural|urban)\s+(?:distribution\s+)?feeder',sentence,re.I):
                if spec.scenario.kind!=match[1].lower():issues.append('Explicit feeder scene differs from the model.')
        for atom in numeric_source_atoms(sentence):
            for value in explicit_pv_values(atom):
                if spec.network_kind!='distribution' or not math.isclose(spec.pv_ratio,value,abs_tol=1e-9):
                    issues.append('Explicit PV directive differs from the compiled model.')
            if atom['scope'] not in ('current','bounded'):continue
            for match in re.finditer(r'(?<![\d.])('+NUMBER+')\\s*(?:kV|\u5343\u4f0f)(?![A-Za-z])',atom['text'],re.I):
                levels={v.kv for v in spec.voltage_layers} if spec.network_kind=='transmission' and spec.voltage_layers else {spec.voltage_kv}
                if float(match[1]) not in levels:issues.append('Explicit voltage differs from the compiled model.')
            power='(?:\u603b(?:\u6709\u529f)?(?:\u8d1f\u8377|\u529f\u7387)|total\\s+(?:active\\s+)?(?:load|demand|power))'
            units='\\s*(?P<unit>MW|kW|W|\u5146\u74e6|\u5343\u74e6|\u74e6)(?![A-Za-z])'
            for pattern in (power+GAP+AMOUNT+units,AMOUNT+units+r'\s*'+power):
                for match in re.finditer(pattern,atom['text'],re.I):
                    factor={'mw':1.,'kw':.001,'w':.000001,'\u5146\u74e6':1.,'\u5343\u74e6':.001,'\u74e6':.000001}[match['unit'].lower()]
                    actual=(spec.total_mw,) if spec.network_kind=='transmission' else (spec.total_kw_min,spec.total_kw_max)
                    if spec.network_kind=='distribution':factor*=1000
                    for op,value in numeric_constraints(atom,match):
                        expected=value*factor
                        if not all(math.isclose(v,expected,rel_tol=1e-8,abs_tol=1e-8) if op=='eq' else v>=expected-1e-8 if op=='ge' else v<=expected+1e-8 for v in actual):
                            issues.append('Explicit total demand differs from the compiled model.')
    return issues


def task_source_issues(request,plan):
    from .research_tasks import effective_search
    from .response_language import _layout_permission,_local_permission_issues,_exclusive_layout_permission
    search=effective_search(plan);spec=plan.base_spec;issues=construction_source_issues(request,spec)
    bound=_layout_permission(request)
    if spec.network_kind=='distribution' and _exclusive_layout_permission(request) is not None:
        if plan.preserve_geometry or set(search.allowed_actions)!={'scale_layout'}:
            issues.append('Explicit uniform-only geometry permission must remain in the response search; '
                          'disabled construction repairs do not erase separately authorized response actions.')
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
        ('support_mvar','(?:\u65e0\u529f(?:\u652f\u6491|\u652f\u6301|\u6ce8\u5165|\u8c03\u8282)(?:\u9884\u7b97|\u5bb9\u91cf)?|finite support|(?:reactive(?:[- ]power)?|Q)[ -](?:support|injection)(?: budget)?)'+GAP+AMOUNT+'\\s*(?P<unit>MVAr|kvar|var|\u5146\u4e4f|\u5343\u4e4f|\u4e4f)'),
        ('transfer_mw','(?:\u529f\u7387\u8f6c\u79fb|\u6709\u529f\u8f6c\u79fb|\u8f6c\u79fb\u529f\u7387|transfer(?: magnitude| budget| power)?)'+GAP+AMOUNT+'\\s*(?P<unit>MW|kW|W|\u5146\u74e6|\u5343\u74e6|\u74e6)(?![A-Za-z])'),
        ('transfer_mw',AMOUNT+r'\s*(?P<unit>MW|kW|W)\s+transfer\b'),
        ('total_power','(?:\u603b(?:\u6709\u529f)?(?:\u8d1f\u8377|\u529f\u7387)|total\\s+(?:active\\s+)?(?:load|demand|power))'+GAP+AMOUNT+'\\s*(?P<unit>MW|kW|W|\u5146\u74e6|\u5343\u74e6|\u74e6)(?![A-Za-z])')]
    response_mentions=response_constraints(request)
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
        absolute='(?:\u7075\u654f\u5ea6|sensitivity|\u54cd\u5e94|response)'+GAP+AMOUNT+r'\s*(?P<unit>pu/MVAr|pu/MW|MW/MW)'
        for match in re.finditer(absolute,text,re.I):
            unit={'voltage_control':'pu/mvar','static_pv_impact':'pu/mw','transmission_transfer':'mw/mw'}[plan.task]
            if match['unit'].lower()!=unit:issues.append('Response unit does not match the task measurement.')
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
