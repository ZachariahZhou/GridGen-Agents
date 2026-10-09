"""Conservative source-grounded normalizations, with no new design authority.

Only closed semantic spellings and references to already explicit quantities
are compiled. Unknown words, conflicting references and unbound values remain
errors for the diagnostic agent; they are never discarded.
"""
import json
import re
import math

from .requirement_contract import canonical


def _reference(ledger, field, exclude):
    matches=[r for r in ledger.requirements if r.id!=exclude and r.priority=='hard'
             and r.disposition=='supported' and canonical(r.target_field or '')==field and r.expected_value is not None]
    identities={json.dumps([r.expected_value,r.operator],sort_keys=True,ensure_ascii=False) for r in matches}
    return matches[0] if len(identities)==1 else None


def _preservation_fields(text, family):
    if not re.search('\u4fdd\u6301|\u4e0d\u53d8|\u4e0d\u5f97\u6539\u53d8|\u4e0d\u80fd\u6539\u53d8|\u4e0d\u8981\u964d\u4f4e|\u4e0d\u5f97\u964d\u4f4e|\u4e0d\u8981\u51cf\u5c11',text) or re.search(r'\d',text):return []
    if '\u6216' in text and not re.search('\u4e0d\u8981\u964d\u4f4e|\u4e0d\u5f97\u964d\u4f4e|\u4e0d\u8981\u51cf\u5c11',text):return []
    names={
        '\u603b\u6709\u529f\u8d1f\u8377':'load','\u603b\u8d1f\u8377':'load','\u8d1f\u8377':'load','\u8d1f\u8377\u603b\u91cf':'load',
        '\u5149\u4f0f\u88c5\u673a\u5bb9\u91cf':'pv_ratio','\u5149\u4f0f\u5bb9\u91cf':'pv_ratio','\u5149\u4f0f\u914d\u7f6e':'pv_ratio','\u5149\u4f0f':'pv_ratio',
        '\u603b\u6bcd\u7ebf\u6570':'n_buses','\u6bcd\u7ebf\u603b\u6570':'n_buses','\u6bcd\u7ebf\u6570\u91cf':'n_buses','\u6bcd\u7ebf\u6570':'n_buses','\u8282\u70b9\u6570':'n_buses',
        '\u53d1\u7535\u673a\u7c7b\u578b':'generator_types','\u673a\u7ec4\u7c7b\u578b':'generator_types','\u5404\u7535\u538b\u5c42\u6570\u91cf':'voltage_layers',
        '\u914d\u53d8\u6570':'transformers','\u914d\u53d8\u6570\u91cf':'transformers','\u7528\u6237\u6570\u91cf':'users','\u7528\u6237\u6570':'users',
    }
    pattern='|'.join(re.escape(n) for n in sorted(names,key=len,reverse=True))
    matches=list(re.finditer(pattern,text))
    if not matches:return []
    # Whole noun phrases must be separated by list connectors. Adjacent nouns
    # such as the number of PV customers describe a different, unbound quantity.
    modifier='(?:\u4e0d\u5f97\u6539\u53d8|\u4e0d\u80fd\u6539\u53d8|\u4e0d\u8981\u964d\u4f4e|\u4e0d\u5f97\u964d\u4f4e|\u4e0d\u8981\u51cf\u5c11|\u4fdd\u6301|\u4e0d\u53d8|\u6307\u5b9a\u503c|\u6307\u5b9a|\u5df2\u7ed9\u5b9a|\u5df2\u660e\u786e|\u73b0\u6709|\u7684|\\s)*'
    if not re.fullmatch(modifier,text[:matches[0].start()]):return []
    if not re.fullmatch(modifier,text[matches[-1].end():]):return []
    for left,right in zip(matches,matches[1:]):
        if not re.fullmatch('\\s*(?:\u548c|\u4e0e|\u53ca|\u6216|\u3001|\uff0c|,)\\s*(?:\u6307\u5b9a|\u5df2\u7ed9\u5b9a|\u5df2\u660e\u786e|\u73b0\u6709)?',text[left.end():right.start()]):return []
    fields=[names[m.group()] for m in matches]
    return list(dict.fromkeys(('total_kw' if family=='hierarchical' else 'total_mw') if f=='load' else f for f in fields))



def _active_clause(request, evidence):
    """A narrow, current affirmative quote; uncertain source stays for review."""
    if evidence not in request:return None
    clauses=[part for part in re.split(r'[。；;\n]',request) if evidence in part]
    if len(clauses)!=1:return None
    clause=clauses[0]
    if re.search('\u539f\u6765|\u6b64\u524d|\u5148\u524d|\u4e4b\u524d|\u539f\u5148|\u65e7\u65b9\u6848|\u6539\u4e3a|\u73b0\u5728\u4ee5|\u4e0d\u8981|\u4e0d\u9700|\u4e0d\u80fd|\u4e0d\u5f97|\u4e0d\u53ef|\u4e0d\u5e94|\u4e0d\u5141\u8bb8|\u7981\u6b62|\u5e76\u975e|\u81f3\u5c11|\u81f3\u591a|\u4e0d\u8d85\u8fc7|\u4e0d\u5c11\u4e8e|\u4ee5\u4e0a|\u4ee5\u4e0b|\u7ea6|\u5927\u6982|\u53ef\u9009|\u6216\u8005|\u6216\u662f|\u8303\u56f4',clause):return None
    if any(c in clause for c in '“”"「」') and re.search('\u5f15\u7528|\u793a\u4f8b|\u4f8b\u5982|\u53ea\u662f|\u65e7\u65b9\u6848',clause):return None
    return clause


def _bus_interval(evidence):
    from .source_review import _counting_range
    band_pattern='(?<![\\d\u96f6\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343])([\\d\u96f6\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343]+)\u591a(?:\u4e2a)?(?:\u6bcd\u7ebf|\u8282\u70b9)'
    typed_pattern='(?<!\\d)(\\d+)\\s*(?:\u5230|\u81f3|[-~\uff5e])\\s*(\\d+)\\s*\u4e2a?(?:\u6bcd\u7ebf|\u8282\u70b9)'
    bands=list(re.finditer(band_pattern,evidence));typed=list(re.finditer(typed_pattern,evidence))
    if len(bands)+len(typed)!=1:return None
    match=(bands or typed)[0]
    if re.search('\u81f3\u5c11|\u81f3\u591a|\u4e0d\u8d85\u8fc7|\u4e0d\u5c11\u4e8e|\u4ee5\u4e0a|\u4ee5\u4e0b|\u7ea6|\u5927\u6982|\u5de6\u53f3|\u8303\u56f4|\u53ef\u9009|\u6216\u8005|\u6216\u662f',evidence):return None
    if bands:return _counting_range(match[1])
    return (int(match[1]),int(match[2])) if 0<int(match[1])<=int(match[2]) else None


def _new_requirement_id(base,suffix,used):
    for index in range(1,1000):
        tail='_'+suffix+'_'+str(index)
        candidate=base[:40-len(tail)]+tail
        if candidate not in used:return candidate
    raise ValueError('No unique requirement ID available')


def _village_bindings(item, family):
    """Compile every atom of a closed village clause, or leave it for review."""
    if family!='single_voltage' or item.operator!='eq':return None
    field=item.target_field
    if field not in {'scenario.topology.family','scenario.topology.branch_count'}:return None
    if field.endswith('.family') and (not isinstance(item.expected_value,str) or item.expected_value not in {'multi_branch','long_trunk'}):return None
    bindings=[];villages=[]
    for part in re.split(r'[，,]',item.evidence):
        part=part.strip()
        village=re.fullmatch('(?:\u4e3b\u5e72\u8fde\u63a5)?\\s*(\\d+)\\s*\u4e2a\u6751\u843d',part)
        phase=re.fullmatch('\u91c7\u7528\u5355\u7535\u538b(\u5e73\u8861(?:\u7b49\u503c)?|\u4e09\u76f8\u4e0d\u5e73\u8861)\u6a21\u578b',part)
        if village:
            count=int(village[1]);villages.append(count)
            bindings.append(('scenario.village_count',count))
        elif phase:bindings.append(('capability.phase_model','unbalanced' if phase[1]=='\u4e09\u76f8\u4e0d\u5e73\u8861' else 'balanced'))
        elif part=='\u4fdd\u6301\u5f84\u5411\u8fd0\u884c':bindings.append(('operating_topology','radial'))
        else:return None
    if len(villages)!=1:return None
    if field.endswith('.branch_count') and item.expected_value!=villages[0]:return None
    return [('scenario.village_count',villages[0])]+[b for b in bindings if b[0]!='scenario.village_count']


def _source_numeric_normalizations(ledger,request):
    changes=[];used={r.id for r in ledger.requirements}
    for item in list(ledger.requirements):
        if item.priority!='hard' or item.disposition!='supported':continue
        clause=_active_clause(request,item.evidence)
        if clause is None:continue
        if canonical(item.target_field or '')=='n_buses':
            interval=_bus_interval(item.evidence)
            if interval is None:
                from .source_review import exact_bus_counts, _integer_count
                match=re.fullmatch('([\\d\u96f6\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343]+)\\s*(?:\u4e2a)?(?:\u6bcd\u7ebf|\u8282\u70b9)',item.evidence)
                exact=_integer_count(match[1]) if match else None
                if exact is None or exact_bus_counts(request)!={exact}:continue
                bounds=[r for r in ledger.requirements if r.priority=='hard' and r.disposition=='supported'
                        and canonical(r.target_field or '')=='n_buses' and r.expected_value is not None]
                if any(type(r.expected_value) not in (int,float) or not math.isfinite(r.expected_value)
                       or r.operator not in ('eq','ge','le') for r in bounds):continue
                lower=max((r.expected_value for r in bounds if r.operator in ('ge','eq')),default=-math.inf)
                upper=min((r.expected_value for r in bounds if r.operator in ('le','eq')),default=math.inf)
                if not lower<=exact<=upper:continue
                if lower==upper==exact:continue
                if item.expected_value is None and item.operator=='eq':
                    old=item.model_dump();item.expected_value=exact
                    changes.append(dict(kind='source_exact_count',before=old,after=item.model_dump()))
                else:
                    identifier=_new_requirement_id(item.id,'source_exact',used);used.add(identifier)
                    new=item.model_copy(update=dict(id=identifier,operator='eq',expected_value=exact))
                    ledger.requirements.append(new)
                    changes.append(dict(kind='source_exact_count',before=None,after=new.model_dump()))
                continue
            lo,hi=interval
            source_span=(re.search('[\\d\u96f6\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343]+\u591a(?:\u4e2a)?(?:\u6bcd\u7ebf|\u8282\u70b9)',item.evidence)
                or re.search('\\d+\\s*(?:\u5230|\u81f3|[-~\uff5e])\\s*\\d+\\s*\u4e2a?(?:\u6bcd\u7ebf|\u8282\u70b9)',item.evidence))
            if source_span is None:continue
            remainder=request.replace(source_span.group(0),'',1)
            from .source_review import _integer_count
            other_mentions=list(re.finditer('(?<![\\d\u96f6\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343])([\\d\u96f6\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343]+)(\u591a)?\\s*(?:\u4e2a)?(?:\u6bcd\u7ebf|\u8282\u70b9)',remainder))
            def independently_fixed(match):
                count=_integer_count(match[1])
                return (not match[2] and count is not None and lo<=count<=hi and
                    any(r.id!=item.id and r.priority=='hard' and r.disposition=='supported' and
                        canonical(r.target_field or '')=='n_buses' and r.operator=='eq' and
                        r.expected_value==count for r in ledger.requirements))
            if any(not independently_fixed(match) for match in other_mentions):continue
            structured=item.expected_value
            structured_matches=(isinstance(structured,dict) and structured=={'ge':lo,'le':hi}) or (
                isinstance(structured,list) and structured==[lo,hi])
            if structured_matches and item.operator in ('eq','ge','le'):
                competing=[r for r in ledger.requirements if r.id!=item.id and r.priority=='hard'
                    and r.disposition=='supported' and canonical(r.target_field or '')=='n_buses']
                if any(type(r.expected_value) not in (int,float) or not math.isfinite(r.expected_value)
                       or r.operator not in ('eq','ge','le') for r in competing):continue
                lower=max((r.expected_value for r in competing if r.operator in ('eq','ge')),default=-math.inf)
                upper_bound=min((r.expected_value for r in competing if r.operator in ('eq','le')),default=math.inf)
                if lower>hi or upper_bound<lo or lower>upper_bound:continue
                old=item.model_dump();item.expected_value=lo;item.operator='ge'
                identifier=_new_requirement_id(item.id,'source_le',used);used.add(identifier)
                upper=item.model_copy(update=dict(id=identifier,expected_value=hi,operator='le'))
                ledger.requirements.append(upper)
                changes.append(dict(kind='source_interval',before=old,after=[item.model_dump(),upper.model_dump()]))
                continue
            bounds=[r for r in ledger.requirements if r.priority=='hard' and r.disposition=='supported'
                    and canonical(r.target_field or '')=='n_buses' and r.expected_value is not None]
            if any(type(r.expected_value) not in (int,float) or not math.isfinite(r.expected_value)
                   or r.operator not in ('eq','ge','le') for r in bounds):continue
            lower=max((r.expected_value for r in bounds if r.operator in ('ge','eq')),default=-math.inf)
            upper=min((r.expected_value for r in bounds if r.operator in ('le','eq')),default=math.inf)
            if lower>hi or upper<lo or lower>upper:continue
            missing=[(op,val) for op,val,needed in [('ge',lo,lower<lo),('le',hi,upper>hi)] if needed]
            for op,val in missing:
                if item.expected_value is None and item.operator=='eq':
                    old=item.model_dump();item.operator=op;item.expected_value=val
                    changes.append(dict(kind='source_interval',before=old,after=item.model_dump()))
                else:
                    identifier=_new_requirement_id(item.id,'source_'+op,used)
                    used.add(identifier)
                    new=item.model_copy(update=dict(id=identifier,operator=op,expected_value=val))
                    ledger.requirements.append(new)
                    changes.append(dict(kind='source_interval',before=None,after=new.model_dump()))
        else:
            bindings=_village_bindings(item,ledger.model_family)
            if bindings:
                old=item.model_dump();item.target_field,item.expected_value=bindings[0]
                changes.append(dict(kind='village_count_mapping',before=old,after=item.model_dump()))
                for field,value in bindings[1:]:
                    if any(r.priority=='hard' and r.disposition=='supported' and canonical(r.target_field or '')==canonical(field)
                           and r.operator=='eq' and r.expected_value==value for r in ledger.requirements):continue
                    identifier=_new_requirement_id(item.id,'source_atom',used);used.add(identifier)
                    atom=item.model_copy(update=dict(id=identifier,target_field=field,expected_value=value))
                    ledger.requirements.append(atom)
                    changes.append(dict(kind='compound_source_atom',before=None,after=atom.model_dump()))
    return changes


def _closed_source_context(item, request, ledger, allowed_atoms=()):
    """Do not erase an unbound neighboring atom merely by quoting a substring."""
    clause=_active_clause(request,item.evidence)
    if clause is None:return False
    def without_connector(text):
        return re.sub('^(?:\u5e76\u4e14|\u540c\u65f6|\u5e76|\u4e14)', '', text.strip()).strip()
    for part in re.split(r'[，,]',clause):
        atom=without_connector(part)
        if atom==item.evidence or atom in allowed_atoms:continue
        if not any(r.id!=item.id and r.priority=='hard' and r.disposition=='supported'
                   and r.target_field and r.expected_value is not None
                   and without_connector(r.evidence)==atom for r in ledger.requirements):return False
    return True


def _normalize_decision_requirement(item,request,ledger):
    field=canonical(item.target_field or '')
    if field not in {'lv_installation_decision','lv_installation_decision.selected','lv_installation_decision.reason'}:return False
    clause=_active_clause(request,item.evidence)
    if clause is None or not _closed_source_context(item,request,ledger):return False
    remainder=request.replace(item.evidence,'',1)
    extra_criterion=re.search('(?:\u5fc5\u987b|\u5e94\u5f53|\u9700\u8981|\u8981\u6c42|\u4e0d\u5f97|\u7981\u6b62)\\s*(?:\u6bd4\u8f83|\u8003\u8651|\u5206\u6790|\u89e3\u91ca|\u8bf4\u660e)[^\u3002\uff1b;]*?(?:\u73af\u5883|\u6210\u672c|\u53ef\u9760\u6027|\u666f\u89c2|\u8d70\u5eca|\u7ef4\u62a4|\u571f\u58e4|\u65bd\u5de5)',remainder)
    if extra_criterion:return False
    if re.search('\u5fc5\u987b|\u5e94\u5f53|\u9700\u8981|\u8981\u6c42|\u4e0d\u5f97|\u7981\u6b62',clause.replace(item.evidence,'',1)):return False
    # Any other mention of a construction method needs its own hard binding;
    # this is intentionally independent of verbs such as adopt, select or be.
    for atom in re.split(r'[，,。；;\n]',remainder):
        if not re.search('\u6392\u7ba1|\u76f4\u57cb|\u67b6\u7a7a|buried_duct|buried_direct|aerial_bundle',atom):continue
        if not any(r.id!=item.id and r.priority=='hard' and r.disposition=='supported'
                   and canonical(r.target_field or '')=='installations' and r.expected_value is not None
                   and r.evidence.strip()==atom.strip() for r in ledger.requirements):return False
    if not re.fullmatch('\u6577\u8bbe\u65b9\u5f0f\u548c\u8bbe\u5907\u5408\u7406\u9009\u62e9\u5e76\u89e3\u91ca|\u6577\u8bbe\u65b9\u5f0f\u5408\u7406\u9009\u62e9\u5e76\u89e3\u91ca|\u6577\u8bbe\u65b9\u5f0f\u5408\u7406\u9009\u62e9\u5e76\u8bf4\u660e',item.evidence):return False
    item.target_field='hierarchy.lv_installation_decision' if item.target_field.startswith('hierarchy.') else 'lv_installation_decision'
    item.expected_value=True;item.operator='eq'
    return True

def reference_context_only(request, evidence):
    """Recognize only a closed historical count and its explicit disclaimer."""
    if not evidence or evidence not in request:return False
    pattern=('(?:\u53c2\u8003(?:\u6587\u732e|\u8d44\u6599)(?:\u4e2d)?\u7684)?(?:\u65e7\u65b9\u6848|\u539f\u65b9\u6848)(?:\u6709|\u4e3a)'
             '\\s*\\d+\\s*\u4e2a?(?:\u6bcd\u7ebf|\u8282\u70b9)[\uff0c,]\\s*'
             '(?:\u8be5\u6570\u5b57|\u8fd9\u4e2a\u6570\u5b57|\u8be5\u6570\u91cf)\u4e0d\u662f\u672c\u6b21\u8981\u6c42')
    spans=[]
    for segment in re.finditer(r'[^。；;\n]+',request):
        if re.fullmatch(pattern,segment.group().strip()):spans.append(segment.span())
    # Repeated text in a current clause must not inherit reference status.
    occurrences=list(re.finditer(re.escape(evidence),request))
    return bool(occurrences) and all(any(start<=m.start() and m.end()<=end for start,end in spans)
                                   for m in occurrences)


def normalize_reference_context(ledger, request):
    changes=[]
    for item in ledger.requirements:
        if (item.priority=='hard' and item.disposition=='supported' and
                item.target_field is None and item.expected_value is None and
                reference_context_only(request,item.evidence)):
            old=item.model_dump();item.priority='preference'
            changes.append(dict(kind='explicit_reference_context',before=old,after=item.model_dump()))
    return changes


def _nonnumeric_label(value):
    if not isinstance(value,str) or not value.strip():return False
    try:float(value)
    except ValueError:return True
    return False


def normalize_ledger(ledger, family, request):
    changes=normalize_reference_context(ledger,request)
    if ledger.model_family is None and (ledger.network_kind,family) in {('distribution','single_voltage'),('distribution','hierarchical'),('transmission','transmission')}:
        ledger.model_family=family
        changes.append(dict(kind='missing_route_metadata',before=None,after=family))
    # A preservation instruction does not become an unsupported capability
    # merely because two other requirements already require clarification.
    conflicts=[r for r in ledger.requirements if r.priority=='hard' and r.disposition=='clarify' and r.reason and r.evidence in request]
    if len(conflicts)>=2:
        for item in ledger.requirements:
            if (item.priority=='hard' and item.disposition=='unsupported' and item.evidence in request and
                re.fullmatch('(?:\u8fd9|\u4e0a\u8ff0|\u4ee5\u4e0a)\u4e24\u6761(?:\u8981\u6c42|\u7ea6\u675f)?(?:\u90fd|\u5747)?(?:\u4e0d\u80fd|\u4e0d\u53ef|\u4e0d\u5f97)\u4fee\u6539',item.evidence)):
                old=item.model_dump();item.disposition='clarify'
                changes.append(dict(kind='preservation_of_conflicting_requirements',before=old,after=item.model_dump()))
    changes.extend(_source_numeric_normalizations(ledger,request))
    # These are model representation/capability labels, not numeric design defaults.
    for item in ledger.requirements:
        if item.evidence not in request or item.priority!='hard' or item.disposition!='supported':continue
        old=item.model_dump()
        if _normalize_decision_requirement(item,request,ledger):
            changes.append(dict(kind='decision_requirement',before=old,after=item.model_dump()))
            old=item.model_dump()
        if (family=='transmission' and canonical(item.target_field or '')=='voltage_layers'
                and isinstance(item.expected_value,str) and item.operator in ('eq','contains')
                and _closed_source_context(item,request,ledger,('\u5408\u7406\u5206\u914d\u5404\u5c42\u6bcd\u7ebf',))
                and re.fullmatch('(?:\u663e\u5f0f(?:\u8bbe\u7f6e|\u914d\u7f6e|\u5efa\u7acb|\u91c7\u7528)?)?\u5c42\u95f4(?:\u663e\u5f0f)?\u53d8\u538b\u5668',item.evidence)):
            item.target_field='transmission.transformer_count';item.expected_value=1;item.operator='ge'
        if (family=='transmission' and canonical(item.target_field or '')=='voltage_layers'
                and _nonnumeric_label(item.expected_value) and item.operator in ('eq','contains')
                and item.evidence=='\u7535\u538b\u5c42\u4e4b\u95f4\u6709\u663e\u5f0f\u53d8\u538b\u5668'):
            clause=_active_clause(request,item.evidence)
            from .source_intent import source_atoms
            matches=[atom for atom in source_atoms(request) if atom['text']==item.evidence]
            if (clause is not None and item.evidence in [part.strip() for part in re.split(r'[，,]',clause)]
                    and matches and all(atom['scope']=='current' for atom in matches)):
                item.target_field='transmission.transformer_count';item.expected_value=1;item.operator='ge'
        if item.target_field=='excluded_deliverables':
            item.target_field='deliverables';item.operator='excludes'
        if canonical(item.target_field or '')=='deliverables':
            def format_name(value):
                if not isinstance(value,str):return value
                match=re.fullmatch('\\s*(opendss|matpower|json|csv)(?:[ _]*model|\u6a21\u578b|\u683c\u5f0f)?\\s*',value,re.I)
                return match.group(1).lower() if match else value
            item.expected_value=([format_name(v) for v in item.expected_value] if isinstance(item.expected_value,list)
                                 else format_name(item.expected_value))
        phase=re.fullmatch('(?:\u751f\u6210|\u8bbe\u8ba1|\u4e00\u4e2a|\u4e00\u6761|\u57ce\u5e02|\u519c\u6751|\u79d1\u7814|\u7814\u7a76|\u7528|\u7684)*\u4e09\u76f8\u4e0d\u5e73\u8861(?:\u79d1\u7814|\u7814\u7a76|\u7528|\u7684|\u914d\u7535\u7f51|\u914d\u7535\u6a21\u578b|\u9988\u7ebf|\u6a21\u578b|\u7cfb\u7edf)*',item.evidence)
        if phase and canonical(item.target_field or '') in ('','phase_weights','phase_model'):
            item.target_field='capability.phase_model';item.expected_value='unbalanced';item.operator='eq'
        # A complete historical-frequency correction clause. The source supplies
        # the current value; the tool's fixed frequency is checked independently.
        frequency=re.fullmatch('(?:\u53c2\u8003\u8d44\u6599|\u53c2\u8003\u6570\u636e|\u65e7\u8d44\u6599)(?:\u91cc|\u4e2d)?(?:\u6709|\u91c7\u7528|\u4f7f\u7528)?\\s*\\d+(?:\\.\\d+)?\\s*Hz[\uff0c,\u3001 ]*(?:\u4f46)?(?:\u8fd9\u6b21|\u6b64\u6b21|\u672c\u6b21)(?:\u751f\u6210|\u91c7\u7528|\u4f7f\u7528|\u7528|\u4e3a)\\s*(\\d+(?:\\.\\d+)?)\\s*Hz',item.evidence,re.I)
        if frequency and canonical(item.target_field or '') in ('','frequency_hz'):
            item.target_field='capability.frequency_hz';item.expected_value=float(frequency.group(1));item.operator='eq'
        if item.model_dump()!=old:changes.append(dict(kind='semantic_alias',before=old,after=item.model_dump()))
    result=[]
    for item in ledger.requirements:
        fields=_preservation_fields(item.evidence,family) if item.priority=='hard' and item.disposition=='supported' and (not item.target_field or item.expected_value is None) else []
        if not fields and item.priority=='hard' and item.disposition=='supported' and item.expected_value is None:
            # A model may quote a whole sentence for one preservation binding.
            # Narrow to the preservation clause only when every other complete
            # clause already has its own explicit, supported ledger binding.
            parts=[s.strip() for s in re.split(r'[，,；;。]',item.evidence) if s.strip()]
            preserved=[(s,_preservation_fields(s,family)) for s in parts if _preservation_fields(s,family)]
            if len(parts)>1 and len(preserved)==1:
                clause,bindings=preserved[0]
                covered=all(any(r.id!=item.id and r.evidence==part and r.disposition=='supported' and
                    r.priority in ('hard','permission') and r.target_field and r.expected_value is not None
                    for r in ledger.requirements) for part in parts if part!=clause)
                target=canonical(item.target_field or '')
                if covered and (not target or target in bindings):fields=bindings
        atoms=[]
        for field in fields:
            reference=_reference(ledger,field,item.id)
            if reference:
                atoms.append((reference.target_field,reference.expected_value,reference.operator,[reference.id]))
            elif field=='voltage_layers' and family=='transmission':
                count=_reference(ledger,'n_buses',item.id);voltage=_reference(ledger,'voltage_kv',item.id)
                if count and voltage and count.operator==voltage.operator=='eq' and isinstance(count.expected_value,(int,float)) and isinstance(voltage.expected_value,(int,float)):
                    atoms.append(('transmission.voltage_layers',[dict(kv=voltage.expected_value,buses=count.expected_value)],'eq',[count.id,voltage.id]))
                else:break
            else:break
        if not fields or len(atoms)!=len(fields):
            result.append(item);continue
        used={r.id for r in ledger.requirements}|{r.id for r in result}
        replacements=[]
        for i,(target,value,op,refs) in enumerate(atoms):
            identifier=_new_requirement_id(item.id,'binding_'+str(i),used)
            used.add(identifier)
            replacements.append(item.model_copy(update=dict(id=identifier,target_field=target,expected_value=value,operator=op,
                reason='Preserve constraints explicitly stated in the source; referenced ledger entries: '+', '.join(refs))))
        result.extend(replacements)
        changes.append(dict(kind='preservation_references',before=item.model_dump(),after=[r.model_dump() for r in replacements]))
    ledger.requirements=result
    return changes


def count_conflict(candidate):
    """Only a contradiction between two explicit ledger values warrants this action."""
    from .requirements import RequirementLedger
    try:
        value=candidate.get('ledger')
        ledger=RequirementLedger.model_validate(json.loads(value) if isinstance(value,str) else value)
        n=_reference(ledger,'n_buses','');layers=_reference(ledger,'voltage_layers','')
        if n is None or layers is None or n.operator!='eq' or layers.operator!='eq':return False
        if not isinstance(layers.expected_value,list) or not layers.expected_value:return False
        def number(value,integer=False):
            return type(value) in (int,float) and math.isfinite(value) and value>0 and (not integer or int(value)==value)
        if not number(n.expected_value,True):return False
        if any(not isinstance(v,dict) or set(v)!={'kv','buses'} or not number(v['kv']) or not number(v['buses'],True) for v in layers.expected_value):return False
        if len({v['kv'] for v in layers.expected_value})!=len(layers.expected_value):return False
        return sum(v['buses'] for v in layers.expected_value)!=n.expected_value
    except (TypeError,ValueError,KeyError):return False
