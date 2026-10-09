"""Source spans and conservative assertion scopes, never inferred requirements.

The atoms are bookkeeping for coverage and deterministic checks. They do not
claim to parse arbitrary language: compound quotes and ambiguous scopes still
need the independent semantic reviewer.
"""
import re


_BOUNDARY = re.compile(
    '[\uff0c,]|(?<!\\d)\\.(?=\\s|$)|\u5e76\u4e14|\u800c\u4e14|\u4f46\u662f|\u540c\u65f6|\u4ee5\u53ca|\u4f46|\u4e14|\u800c|'
    '\u548c(?=\u4e0d|\u65e0|\u52ff|\u5fc5\u987b|\u8981\u6c42|\u6240\u6709)|\\b(?:and|but)\\b|'
    '(?=\u672c\u6b21|\u8fd9\u6b21|\u6b64\u6b21|\u73b0\u5728|\u73b0\u6539\u4e3a|\u6539\u4e3a|\u6539\u6210)', re.I)
_REFERENCE = re.compile(
    '\u793a\u4f8b|\u4f8b\u5982|\u5f15\u7528|\u53c2\u8003(?:\u8d44\u6599|\u6570\u636e|\u6587\u732e|\u6807\u9898|\u65b9\u6848|\u6a21\u578b)|\u539f\u65b9\u6848|\u65e7\u65b9\u6848|\u539f\u6765|\u6b64\u524d|\u5148\u524d|\u4e4b\u524d|\u539f\u5148|'
    r'\b(?:examples?|previous(?:ly)?|formerly|reference|historical|old\s+(?:plan|model|case))\b', re.I)
_CURRENT = re.compile('\u672c\u6b21|\u8fd9\u6b21|\u6b64\u6b21|\u73b0\u5728|\u5f53\u524d|\u6539\u4e3a|\u6539\u6210|^(?:\u751f\u6210|\u8bbe\u8ba1|\u91c7\u7528|\u4f7f\u7528)|\\b(?:now|this\\s+time|currently|instead|generate|create|use|design)\\b', re.I)
_NEGATIVE = re.compile(
    '\u4e0d\u8981|\u4e0d\u9700|\u4e0d\u8981\u6c42|\u4e0d\u80fd|\u4e0d\u5f97|\u4e0d\u53ef|\u4e0d\u5e94|\u4e0d\u5141\u8bb8|\u7981\u6b62|\u5e76\u975e|\u4e0d\u662f|\u4e0d\u8bbe|\u4e0d\u914d|\u4e0d\u542b|\u4e0d\u5e26|\u4e0d\u5305\u542b|\u4e0d\u5305\u62ec|\u52ff|\u65e0(?:\u5149\u4f0f|PV)|'
    r"\b(?:not|no|without|never|exclude|excluding)\b|\b(?:don['’]t|mustn['’]t)\b", re.I)
_GE = '\u81f3\u5c11|\u4e0d\u5c11\u4e8e|\u4e0d\u4f4e\u4e8e|\u4e0d\u5c0f\u4e8e|>=|\u2265|\\bat\\s+least\\b|\\bno\\s+less\\s+than\\b|\\bminimum\\b'
_LE = '\u81f3\u591a|\u6700\u591a|\u4e0d\u8d85\u8fc7|\u4e0d\u9ad8\u4e8e|\u4e0d\u5927\u4e8e|<=|\u2264|\\bat\\s+most\\b|\\bno\\s+more\\s+than\\b|\\bmaximum\\b'
_BOUND = re.compile(_GE+'|'+_LE+'|\u4ee5\u4e0a|\u4ee5\u4e0b|\u4ee5\u5185|\u8303\u56f4|\\bbetween\\b',re.I)
_UNCERTAIN = re.compile(
    '\u7ea6|\u5927\u6982|\u5de6\u53f3|\u6216\u8005|\u6216\u662f|(?:^|\\s)\u6216|\u53ef\u9009|\u4efb\u9009|\u5747\u53ef|\u90fd\u53ef|\u5982\u679c|\u5047\u8bbe|\u82e5\u662f|'
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
            # Keep the subject of a revision: changing MV to 9 buses is regional,
            # and changing total demand to 48 kW still names power. Split a superseded numeric
            # assertion only when there actually is an earlier value.
            if (not match.group() and re.match('\u6539\u4e3a|\u6539\u6210',text[match.start():])
                    and not re.search('[\\d\u96f6\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343]',text[last_boundary:match.start()])):
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
                    negative and not bounded and connector.lower() in ('and','\u4ee5\u53ca','\u5e76\u4e14','\u4e14'))
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
    text=re.sub('^(?:(?:\u672c\u6b21|\u8fd9\u6b21|\u6b64\u6b21|\u73b0\u5728|\u5f53\u524d|\u6539\u4e3a|\u6539\u6210|\u8bf7|this\\s+time|now|currently)\\s*)+','',atom['text'],flags=re.I)
    text=text.strip().rstrip('.!?。！？').strip()
    noun='(?:\u5149\u4f0f(?:\u53d1\u7535|\u88c5\u673a)?|PV|photovoltaics?|solar\\s+(?:PV|generation))'
    prohibition=('(?:(?:\u4e0d\u8981|\u4e0d\u9700\u8981|\u4e0d\u9700|\u7981\u6b62|\u4e0d\u5f97|\u4e0d\u5141\u8bb8|\u52ff)(?:\u8bbe\u7f6e|\u914d\u7f6e|\u63a5\u5165|\u5b89\u88c5|\u88c5\u8bbe)?|'
        '\u4e0d(?:\u8bbe\u7f6e|\u8bbe|\u63a5\u5165|\u63a5|\u914d\u7f6e|\u914d|\u5b89\u88c5|\u88c5\u8bbe|\u542b|\u5e26|\u5305\u542b|\u5305\u62ec)|\u65e0)\\s*(?:\u4efb\u4f55)?\\s*'+noun+
        r'|(?:no|without)\s+(?:any\s+)?'+noun+
        r"|(?:do\s+not|don['’]t|must\s+not|never)\s+(?:include|install|use|add|connect)\s+(?:any\s+)?"+noun)
    if re.fullmatch(prohibition,text,re.I):return [0.]
    # A current model may carry its exclusion as an adjectival modifier.
    # Preserve outer negation and hypothetical/reference scope; 'not a model
    # without PV' cannot be compiled as a prohibition of PV.
    modifier=re.search('(?:\u4e0d\u5305\u542b|\u4e0d\u5305\u62ec|\u4e0d\u542b|\u4e0d\u5e26)\\s*(?:\u4efb\u4f55)?\\s*'+noun+'\u7684',text,re.I)
    if modifier:
        prefix=text[:modifier.start()];suffix=text[modifier.end():]
        model_noun=re.search('\u9988\u7ebf|\u914d\u7535\u7f51|\u7535\u7f51|\u7f51\u7edc|\u6a21\u578b|\u7cfb\u7edf|\\b(?:case|model|feeder|network)\\b',suffix,re.I)
        if model_noun and not _NEGATIVE.search(prefix) and not _NEGATIVE.search(suffix):return [0.]
    # A generation directive need not put a comma before its final no-PV
    # qualifier. Reject nested negations instead of flattening them into zero.
    suffix=re.search(r'(?:'+prohibition+r')$',text,re.I)
    if suffix:
        prefix=text[:suffix.start()]
        if (re.search('\u751f\u6210|\u8bbe\u8ba1|\u521b\u5efa|\u6784\u5efa|\\b(?:generate|design|create|build)\\b',prefix,re.I)
                and not _NEGATIVE.search(prefix) and not _REFERENCE.search(prefix)):
            return [0.]
    if atom['scope']=='negative':return []
    label='(?:\u5149\u4f0f(?:\u603b\u6709\u529f|\u6bd4\u4f8b|\u5360\u6bd4|\u5bb9\u91cf|\u88c5\u673a\u5bb9\u91cf)?|PV(?:\\s+(?:capacity|ratio))?)'
    assignment='\\s*(?:\u4e3a|\u662f|\u7b49\u4e8e|is|must\\s+be|=|\uff1a|:)?\\s*'
    denominator='(?:(?:\u603b\u6709\u529f\u8d1f\u8377|\u603b\u8d1f\u8377)\u7684?|of\\s+(?:the\\s+)?(?:total\\s+)?load)?\\s*'
    percentage=re.fullmatch(label+assignment+denominator+r'(\d+(?:\.\d+)?)\s*%',text,re.I)
    if percentage:return [float(percentage[1])/100.]
    ratio=re.fullmatch('(?:\u5149\u4f0f(?:\u6bd4\u4f8b|\u5360\u6bd4)|PV\\s+ratio)'+assignment+r'(\d+(?:\.\d+)?)',text,re.I)
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
    if re.search(_GE,before,re.I) or re.match('\\s*(?:\u53ca)?\u4ee5\u4e0a',after):return [('ge',value)]
    if re.search(_LE,before,re.I) or re.match('\\s*(?:(?:\u53ca)?\u4ee5\u4e0b|\u4ee5\u5185)',after):return [('le',value)]
    return [('eq',value)]
