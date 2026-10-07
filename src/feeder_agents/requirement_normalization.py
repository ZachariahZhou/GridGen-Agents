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
    if not re.search(r'保持|不变|不得改变|不能改变|不要降低|不得降低|不要减少',text) or re.search(r'\d',text):return []
    if '或' in text and not re.search(r'不要降低|不得降低|不要减少',text):return []
    names={
        '总有功负荷':'load','总负荷':'load','负荷':'load','负荷总量':'load',
        '光伏装机容量':'pv_ratio','光伏容量':'pv_ratio','光伏配置':'pv_ratio','光伏':'pv_ratio',
        '总母线数':'n_buses','母线总数':'n_buses','母线数量':'n_buses','母线数':'n_buses','节点数':'n_buses',
        '发电机类型':'generator_types','机组类型':'generator_types','各电压层数量':'voltage_layers',
        '配变数':'transformers','配变数量':'transformers','用户数量':'users','用户数':'users',
    }
    pattern='|'.join(re.escape(n) for n in sorted(names,key=len,reverse=True))
    matches=list(re.finditer(pattern,text))
    if not matches:return []
    # Whole noun phrases must be separated by list connectors. Adjacent nouns
    # such as 光伏用户数量 describe a different, unbound quantity.
    modifier=r'(?:不得改变|不能改变|不要降低|不得降低|不要减少|保持|不变|指定值|指定|已给定|已明确|现有|的|\s)*'
    if not re.fullmatch(modifier,text[:matches[0].start()]):return []
    if not re.fullmatch(modifier,text[matches[-1].end():]):return []
    for left,right in zip(matches,matches[1:]):
        if not re.fullmatch(r'\s*(?:和|与|及|或|、|，|,)\s*(?:指定|已给定|已明确|现有)?',text[left.end():right.start()]):return []
    fields=[names[m.group()] for m in matches]
    return list(dict.fromkeys(('total_kw' if family=='hierarchical' else 'total_mw') if f=='load' else f for f in fields))



def _active_clause(request, evidence):
    """A narrow, current affirmative quote; uncertain source stays for review."""
    if evidence not in request:return None
    clauses=[part for part in re.split(r'[。；;\n]',request) if evidence in part]
    if len(clauses)!=1:return None
    clause=clauses[0]
    if re.search(r'原来|此前|先前|之前|原先|旧方案|改为|现在以|不要|不需|不能|不得|不可|不应|不允许|禁止|并非|至少|至多|不超过|不少于|以上|以下|约|大概|可选|或者|或是|范围',clause):return None
    if any(c in clause for c in '“”"「」') and re.search(r'引用|示例|例如|只是|旧方案',clause):return None
    return clause


def _bus_interval(evidence):
    from .source_review import _counting_range
    band_pattern=r'(?<![\d零一二两三四五六七八九十百千])([\d零一二两三四五六七八九十百千]+)多(?:个)?(?:母线|节点)'
    typed_pattern=r'(?<!\d)(\d+)\s*(?:到|至|[-~～])\s*(\d+)\s*个?(?:母线|节点)'
    bands=list(re.finditer(band_pattern,evidence));typed=list(re.finditer(typed_pattern,evidence))
    if len(bands)+len(typed)!=1:return None
    match=(bands or typed)[0]
    if re.search(r'至少|至多|不超过|不少于|以上|以下|约|大概|左右|范围|可选|或者|或是',evidence):return None
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
        village=re.fullmatch(r'(?:主干连接)?\s*(\d+)\s*个村落',part)
        phase=re.fullmatch(r'采用单电压(平衡(?:等值)?|三相不平衡)模型',part)
        if village:
            count=int(village[1]);villages.append(count)
            bindings.append(('scenario.village_count',count))
        elif phase:bindings.append(('capability.phase_model','unbalanced' if phase[1]=='三相不平衡' else 'balanced'))
        elif part=='保持径向运行':bindings.append(('operating_topology','radial'))
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
                match=re.fullmatch(r'([\d零一二两三四五六七八九十百千]+)\s*(?:个)?(?:母线|节点)',item.evidence)
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
            source_span=(re.search(r'[\d零一二两三四五六七八九十百千]+多(?:个)?(?:母线|节点)',item.evidence)
                or re.search(r'\d+\s*(?:到|至|[-~～])\s*\d+\s*个?(?:母线|节点)',item.evidence))
            if source_span is None:continue
            remainder=request.replace(source_span.group(0),'',1)
            from .source_review import _integer_count
            other_mentions=list(re.finditer(r'(?<![\d零一二两三四五六七八九十百千])([\d零一二两三四五六七八九十百千]+)(多)?\s*(?:个)?(?:母线|节点)',remainder))
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
        return re.sub(r'^(?:并且|同时|并|且)', '', text.strip()).strip()
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
    extra_criterion=re.search(r'(?:必须|应当|需要|要求|不得|禁止)\s*(?:比较|考虑|分析|解释|说明)[^。；;]*?(?:环境|成本|可靠性|景观|走廊|维护|土壤|施工)',remainder)
    if extra_criterion:return False
    if re.search(r'必须|应当|需要|要求|不得|禁止',clause.replace(item.evidence,'',1)):return False
    # Any other mention of a construction method needs its own hard binding;
    # this is intentionally independent of verbs such as 采用, 选用 or 为.
    for atom in re.split(r'[，,。；;\n]',remainder):
        if not re.search(r'排管|直埋|架空|buried_duct|buried_direct|aerial_bundle',atom):continue
        if not any(r.id!=item.id and r.priority=='hard' and r.disposition=='supported'
                   and canonical(r.target_field or '')=='installations' and r.expected_value is not None
                   and r.evidence.strip()==atom.strip() for r in ledger.requirements):return False
    if not re.fullmatch(r'敷设方式和设备合理选择并解释|敷设方式合理选择并解释|敷设方式合理选择并说明',item.evidence):return False
    item.target_field='hierarchy.lv_installation_decision' if item.target_field.startswith('hierarchy.') else 'lv_installation_decision'
    item.expected_value=True;item.operator='eq'
    return True

def reference_context_only(request, evidence):
    """Recognize only a closed historical count and its explicit disclaimer."""
    if not evidence or evidence not in request:return False
    pattern=(r'(?:参考(?:文献|资料)(?:中)?的)?(?:旧方案|原方案)(?:有|为)'
             r'\s*\d+\s*个?(?:母线|节点)[，,]\s*'
             r'(?:该数字|这个数字|该数量)不是本次要求')
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
                re.fullmatch(r'(?:这|上述|以上)两条(?:要求|约束)?(?:都|均)?(?:不能|不可|不得)修改',item.evidence)):
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
                and _closed_source_context(item,request,ledger,('合理分配各层母线',))
                and re.fullmatch(r'(?:显式(?:设置|配置|建立|采用)?)?层间(?:显式)?变压器',item.evidence)):
            item.target_field='transmission.transformer_count';item.expected_value=1;item.operator='ge'
        if (family=='transmission' and canonical(item.target_field or '')=='voltage_layers'
                and _nonnumeric_label(item.expected_value) and item.operator in ('eq','contains')
                and item.evidence=='电压层之间有显式变压器'):
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
                match=re.fullmatch(r'\s*(opendss|matpower|json|csv)(?:[ _]*model|模型|格式)?\s*',value,re.I)
                return match.group(1).lower() if match else value
            item.expected_value=([format_name(v) for v in item.expected_value] if isinstance(item.expected_value,list)
                                 else format_name(item.expected_value))
        phase=re.fullmatch(r'(?:生成|设计|一个|一条|城市|农村|科研|研究|用|的)*三相不平衡(?:科研|研究|用|的|配电网|配电模型|馈线|模型|系统)*',item.evidence)
        if phase and canonical(item.target_field or '') in ('','phase_weights','phase_model'):
            item.target_field='capability.phase_model';item.expected_value='unbalanced';item.operator='eq'
        # A complete historical-frequency correction clause. The source supplies
        # the current value; the tool's fixed frequency is checked independently.
        frequency=re.fullmatch(r'(?:参考资料|参考数据|旧资料)(?:里|中)?(?:有|采用|使用)?\s*\d+(?:\.\d+)?\s*Hz[，,、 ]*(?:但)?(?:这次|此次|本次)(?:生成|采用|使用|用|为)\s*(\d+(?:\.\d+)?)\s*Hz',item.evidence,re.I)
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
                reason='保持已由原文明示的约束；引用台账项：'+', '.join(refs))))
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
