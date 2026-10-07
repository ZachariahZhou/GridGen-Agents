"""Source spans and conservative assertion scopes, never inferred requirements.

The atoms are bookkeeping for coverage and deterministic checks. They do not
claim to parse arbitrary language: compound quotes and ambiguous scopes still
need the independent semantic reviewer.
"""
import re


_BOUNDARY = re.compile(
    r'[，,]|(?<!\d)\.(?=\s|$)|并且|而且|但是|同时|以及|但|且|而|'
    r'和(?=不|无|勿|必须|要求|所有)|\b(?:and|but)\b|'
    r'(?=本次|这次|此次|现在|现改为|改为|改成)', re.I)
_REFERENCE = re.compile(
    r'示例|例如|引用|参考(?:资料|数据|文献|标题|方案|模型)|原方案|旧方案|原来|此前|先前|之前|原先|'
    r'\b(?:examples?|previous(?:ly)?|formerly|reference|historical|old\s+(?:plan|model|case))\b', re.I)
_CURRENT = re.compile(r'本次|这次|此次|现在|当前|改为|改成|^(?:生成|设计|采用|使用)|\b(?:now|this\s+time|currently|instead|generate|create|use|design)\b', re.I)
_NEGATIVE = re.compile(
    r'不要|不需|不要求|不能|不得|不可|不应|不允许|禁止|并非|不是|不设|不配|不含|不带|不包含|不包括|勿|无(?:光伏|PV)|'
    r"\b(?:not|no|without|never|exclude|excluding)\b|\b(?:don['’]t|mustn['’]t)\b", re.I)
_GE = r'至少|不少于|不低于|不小于|>=|≥|\bat\s+least\b|\bno\s+less\s+than\b|\bminimum\b'
_LE = r'至多|最多|不超过|不高于|不大于|<=|≤|\bat\s+most\b|\bno\s+more\s+than\b|\bmaximum\b'
_BOUND = re.compile(_GE+'|'+_LE+r'|以上|以下|以内|范围|\bbetween\b',re.I)
_UNCERTAIN = re.compile(
    r'约|大概|左右|或者|或是|(?:^|\s)或|可选|任选|均可|都可|如果|假设|若是|'
    r'\b(?:about|approximately|or|if|suppose|assuming)\b', re.I)


def source_atoms(request):
    """Exact nonempty source spans; quotes and structured lists stay intact."""
    atoms=[]
    for segment_id,segment in enumerate(m for m in re.finditer(r'[^。；;\n]+',request) if m.group().strip()):
        text=segment.group(); protected=[]; stack=[]; quote=None; start=0
        pairs={'“':'”','「':'」','"':'"','(' : ')','（':'）','[':']','{':'}'}
        for index,char in enumerate(text):
            if quote is not None:
                if char==quote:
                    protected.append((start,index+1));quote=None
                continue
            if char in ('“','「','"'):
                start=index;quote=pairs[char]
            elif char in ('(','（','[','{'):
                stack.append((index,pairs[char]))
            elif stack and char==stack[-1][1]:
                opening,_=stack.pop();protected.append((opening,index+1))
        if quote is not None:protected.append((start,len(text)))
        protected.extend((opening,len(text)) for opening,_ in stack)
        boundaries=[];last_boundary=0
        for match in _BOUNDARY.finditer(text):
            if any(a<=match.start()<b for a,b in protected):continue
            if (match.group().lower()=='and' and
                    re.search(r'\bbetween\s+\d+(?:\.\d+)?\s*$',text[:match.start()],re.I)):
                continue  # A numeric interval is one assertion, not two clauses.
            # Keep the subject of a revision: 中压改为9个母线 is regional,
            # and 总负荷改为48kW still names power. Split a superseded numeric
            # assertion only when there actually is an earlier value.
            if (not match.group() and re.match(r'改为|改成',text[match.start():])
                    and not re.search(r'[\d零一二两三四五六七八九十百千]',text[last_boundary:match.start()])):
                continue
            boundaries.append(match);last_boundary=match.end()
        previous=0;connector='';reference=False;negative=False
        for boundary in [*boundaries,None]:
            end=boundary.start() if boundary is not None else len(text)
            chunk=text[previous:end]; stripped=chunk.strip()
            if stripped:
                a=segment.start()+previous+len(chunk)-len(chunk.lstrip())
                b=segment.start()+end-len(chunk)+len(chunk.rstrip())
                current=bool(_CURRENT.search(stripped))
                if current:reference=False;negative=False
                is_reference=bool(_REFERENCE.search(stripped)) or reference
                bounded=bool(_BOUND.search(stripped))
                # 'No more than' is an upper bound, whereas 'do not require
                # at most' negates the requirement itself. Keep that distinction.
                negative_text=re.sub(_GE+'|'+_LE,'',stripped,flags=re.I)
                is_negative=bool(_NEGATIVE.search(negative_text)) or (
                    negative and not bounded and connector.lower() in ('and','以及','并且','且'))
                scope=('reference' if is_reference else 'uncertain' if _UNCERTAIN.search(stripped) else
                       'negative' if is_negative else 'bounded' if bounded else 'current')
                atoms.append(dict(text=request[a:b],start=a,end=b,segment_id=segment_id,scope=scope))
                reference=is_reference;negative=is_negative
            if boundary is not None:
                previous=boundary.end();connector=boundary.group()
    return atoms


def source_coverage(request,ledger):
    """Return unquoted atoms and whether a quote spans multiple obligations."""
    atoms=source_atoms(request);covered=set();compound=False
    for item in ledger.requirements:
        if not item.evidence or item.evidence not in request:continue
        # Repeated literal quotes may refer to several occurrences. Each must
        # still keep its original context; no synthetic source quote is made.
        for match in re.finditer(re.escape(item.evidence),request):
            touched={i for i,atom in enumerate(atoms)
                     if match.start()<atom['end'] and atom['start']<match.end()}
            covered.update(touched)
            compound=compound or len(touched)>1
    return [atom for i,atom in enumerate(atoms) if i not in covered],compound


def explicit_pv_values(atom):
    """Closed current PV directives; unknown qualifiers remain semantic work."""
    if atom['scope'] in ('reference','uncertain'):return []
    text=re.sub(r'^(?:(?:本次|这次|此次|现在|当前|改为|改成|请|this\s+time|now|currently)\s*)+','',atom['text'],flags=re.I)
    text=text.strip().rstrip('.!?。！？').strip()
    noun=r'(?:光伏(?:发电|装机)?|PV|photovoltaics?|solar\s+(?:PV|generation))'
    prohibition=(r'(?:(?:不要|不需要|不需|禁止|不得|不允许|勿)(?:设置|配置|接入|安装|装设)?|'
        r'不(?:设置|设|接入|接|配置|配|安装|装设|含|带|包含|包括)|无)\s*(?:任何)?\s*'+noun+
        r'|(?:no|without)\s+(?:any\s+)?'+noun+
        r"|(?:do\s+not|don['’]t|must\s+not|never)\s+(?:include|install|use|add|connect)\s+(?:any\s+)?"+noun)
    if re.fullmatch(prohibition,text,re.I):return [0.]
    # A current model may carry its exclusion as an adjectival modifier.
    # Preserve outer negation and hypothetical/reference scope; 'not a model
    # without PV' cannot be compiled as a prohibition of PV.
    modifier=re.search(r'(?:不包含|不包括|不含|不带)\s*(?:任何)?\s*'+noun+r'的',text,re.I)
    if modifier:
        prefix=text[:modifier.start()];suffix=text[modifier.end():]
        model_noun=re.search(r'馈线|配电网|电网|网络|模型|系统|\b(?:case|model|feeder|network)\b',suffix,re.I)
        if model_noun and not _NEGATIVE.search(prefix) and not _NEGATIVE.search(suffix):return [0.]
    # A generation directive need not put a comma before its final no-PV
    # qualifier. Reject nested negations instead of flattening them into zero.
    suffix=re.search(r'(?:'+prohibition+r')$',text,re.I)
    if suffix:
        prefix=text[:suffix.start()]
        if (re.search(r'生成|设计|创建|构建|\b(?:generate|design|create|build)\b',prefix,re.I)
                and not _NEGATIVE.search(prefix) and not _REFERENCE.search(prefix)):
            return [0.]
    if atom['scope']=='negative':return []
    label=r'(?:光伏(?:总有功|比例|占比|容量|装机容量)?|PV(?:\s+(?:capacity|ratio))?)'
    assignment=r'\s*(?:为|是|等于|is|must\s+be|=|：|:)?\s*'
    denominator=r'(?:(?:总有功负荷|总负荷)的?|of\s+(?:the\s+)?(?:total\s+)?load)?\s*'
    percentage=re.fullmatch(label+assignment+denominator+r'(\d+(?:\.\d+)?)\s*%',text,re.I)
    if percentage:return [float(percentage[1])/100.]
    ratio=re.fullmatch(r'(?:光伏(?:比例|占比)|PV\s+ratio)'+assignment+r'(\d+(?:\.\d+)?)',text,re.I)
    return [float(ratio[1])] if ratio else []


def numeric_source_atoms(request):
    """Keep affirmative quantities alongside a recognized closed PV exclusion.

    The PV recognizer rejects outer negation, other negative qualifiers and
    noncurrent contexts. Only that closed case has a negative PV predicate and
    affirmative quantities in the same atom; coverage retains the original scope.
    """
    atoms=source_atoms(request)
    for atom in atoms:
        if atom['scope']=='negative' and explicit_pv_values(atom)==[0.]:
            atom['scope']='bounded' if _BOUND.search(atom['text']) else 'current'
    return atoms


def numeric_constraints(atom,match):
    """Preserve exact/range/inequality semantics for a recognized quantity."""
    value=float(match['value'])
    if match.groupdict().get('upper') is not None:
        return [('ge',value),('le',float(match['upper']))]
    before=atom['text'][:match.start('value')]
    before=re.split(r'\d+(?:\.\d+)?',before)[-1]
    after=atom['text'][match.end():]
    if re.search(_GE,before,re.I) or re.match(r'\s*(?:及)?以上',after):return [('ge',value)]
    if re.search(_LE,before,re.I) or re.match(r'\s*(?:(?:及)?以下|以内)',after):return [('le',value)]
    return [('eq',value)]
