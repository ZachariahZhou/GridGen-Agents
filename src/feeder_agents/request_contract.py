"""Conservative checks for explicit deliverables and simultaneous hard constraints.

This is a guard for unambiguous expressions, not a general natural-language parser.
Unmatched wording still goes through the evidence-linked LLM compiler.
"""
import re

PRODUCT_SCOPE = (
    '输配电模型均以工程合理性为目标：需求一致、设备类型和电压适配、参数单位与关联一致，并满足任务规定的电气约束。真实馈线分布和设备目录用于参考，不要求复刻真实网络；用户未要求统计拟合时，不将缺少真实分布校准列为阻塞问题。'
    '本系统交付科研电网模型（配电馈线模型或单/多电压平衡输电网模型）：拓扑、空间位置、相别、设备及用户负荷配置、可视化和可调用模型文件。'
    '不生成8760小时、日/年负荷或光伏时序数据，也不把快照作为生成产品；潮流仅用于内部电气校验。'
    '用户自行提供时序或将馈线用于后续时序研究是允许的，不应拒绝这种网络模型需求。'
    '不交付电磁暂态、保护动作或故障波形模型；仅将静态网络用于用户后续暂态研究是允许的。'
    '三相低压采用接地等效模型，不含显式中性线导体、电位及中性线位移；不能承诺四导体互阻抗模型。'
    '当前生成工具按50Hz建模及换算参数，显式要求其他频率时不能静默使用50Hz替代。'
)

INTENT_GUIDANCE = PRODUCT_SCOPE + (
    '普通多电压反馈支持经授权的局部拓扑调整：同一配变供区内低压用户重接邻近分支，固定节点位置、负荷、相别、电压层级及施工方式。此许可由执行层读取原需求，不要为它创建额外assignment字段；明确保留拓扑时禁止重接。它不是跨配变、任意拓扑重设计或GIS规划。'
    '先逐项检查所有显式要求，再决定是否生成。需要我们生成时序数据时说明交付范围，不能默默用网络模型替代整项请求。'
    '同一基准的矛盾硬约束必须列入blocking_questions，完整写出两个冲突值和单位并询问保留哪项；不得自行取平均、覆盖或选择其中一项。'
    '无光伏与正光伏比例、全网无例外穿管与区域架空、排除全部敷设方式均需指出冲突双方。'
    'unsupported必须解释具体能力边界，不能只复述一个本来支持的要求。50%光伏和无光伏各自都支持。'
    '拒绝或澄清时assignments可为空，无需补设备/敷设分析。'
    'lv_installation_decision与lv_regions必须放入assignments的value，不是意图顶层字段；'
    '例如{"field":"hierarchy.lv_installation_decision","value":{"selected":"aerial_bundle","reason":"研究假设","factors":[],"alternatives":[],"unknowns":[]},"origin":"inferred","reason":"选型"}仅示意嵌套位置；实际alternatives须完整。'
)


def request_guard(text):
    # Exact count intervals can be checked arithmetically without asking an LLM
    # to construct conflicting executable specifications. Do not reinterpret
    # revisions, negations, examples or constraints on different quantities.
    for clause in re.split(r'[。；;\n]',text):
        if re.search(r'不要求|不需要|无需|原先|此前|原来|改为|例如|举例',clause):continue
        interval=re.search(r'((?:用户|节点|母线)(?:数量|总数|数))\s*(?:必须)?\s*(?:至少|不少于|不低于)\s*(\d+)\s*(?:个)?\s*(?:且|并且|同时|和)\s*(?:至多|不超过|最多)\s*(\d+)',clause)
        if interval and int(interval[2])>int(interval[3]):
            issue=f'{interval[1]}下界{interval[2]}大于上界{interval[3]}，两个限制不能同时满足；请明确需要保留的数量范围。'
            return dict(status='needs_clarification',issues=[issue],questions=[issue])
    # Inspect clauses separately: an external-data statement must not hide a
    # separate explicit demand for generated time series.
    for clause in re.split(r'[，,。；;\n]',text):
        frequency=re.search(r'(?:生成|构建|建立|交付)\s*(\d+(?:\.\d+)?)\s*(?:Hz|赫兹)|(?:频率\s*(?:必须|要求)?\s*(?:为|是|=|：|:)?|必须\s*(?:为|使用|用))\s*(\d+(?:\.\d+)?)\s*(?:Hz|赫兹)',clause,re.I)
        if frequency and not re.search(r'不需要|无需|不要求|不要|自行|后续|原先|改为',clause):
            hz=float(next(v for v in frequency.groups() if v is not None))
            if hz!=50:return dict(status='unsupported',issues=[f'明确要求{hz:g}Hz，但当前生成工具仅支持50Hz；不能静默改变所要求的频率。'],questions=[])
        neutral=re.search(r'中性线(?:电位|位移)|neutral (?:potential|displacement)',clause,re.I)
        if neutral and re.search(r'需要|要求|必须|显式|交付|require|explicit',clause,re.I) and not re.search(r'不需要|无需|不要求|不要|自行|后续|用于|do not|no need',clause,re.I):
            return dict(status='unsupported',issues=['当前三相低压工具采用接地等效模型，不支持显式中性线电位或位移；不能以等效模型冒充所要求的四导体模型。'],questions=[])
        transient=re.search(r'暂态|保护动作.*波形|故障.*波形|electromagnetic transient',clause,re.I)
        if transient and re.search(r'生成|输出|交付|提供|模拟|generate|deliver|simulate',clause,re.I) and not re.search(r'不需要|无需|不生成|不要求|不要|自行|自备|后续|用于|do not|no need|my own|provided by',clause,re.I):
            return dict(status='unsupported',issues=['本系统交付静态科研电网模型，不交付暂态/保护动作仿真及故障波形；该显式交付要求超出能力范围。'],questions=[])
        temporal=re.search(r'8760|逐小时|时序|time[ -]?series|hourly',clause,re.I)
        if not temporal:continue
        external=re.search(r'不需要|无需|不生成|不要求|不要|自行|自备|自己提供|由(?:我|用户|我们)提供|我(?:来)?提供|后续|用于|do not|no need|my own|provided by',clause,re.I)
        demand=re.search(r'生成|输出|交付|提供|要求|需要|附带|generate|produce|deliver|supply',clause,re.I)
        if demand and not external:
            return dict(status='unsupported',issues=[PRODUCT_SCOPE+' 本次明确要求生成时序数据，超出交付范围。'],questions=[])
    # A simultaneous 0% versus positive PV share is a clarification, since
    # either request is supported alone. Require complete comma-delimited
    # requirement atoms; a negated/example prefix cannot become a hard value.
    for clause in re.split(r'[。；;\n]',text):
        if re.search(r'旧方案|参考文献|原先|此前|原来|改为|修订|更新为|并非|不是|不再|无需|不要求|可选|可以|或者|或是|不同工况|不同场景|例如|举例|示例|假设|假如|如果|若',clause):
            continue
        labels=re.findall(r'(?:工况|场景|方案)\s*([A-Za-z甲乙一二0-9]+)',clause)
        if len(set(labels))>1:
            continue
        atoms=[part.strip() for part in re.split(r'[，,]',clause) if part.strip()]
        zero=any(re.fullmatch(r'禁止任何光伏接入|不接入光伏|无光伏接入',atom) for atom in atoms)
        positive=[match for atom in atoms if (match:=re.fullmatch(
            r'(?:(?:同时|并且)\s*)?必须接入占总负荷\s*(\d+(?:\.\d+)?)\s*%\s*的光伏',atom))]
        if zero and len(positive)==1 and re.search(r'同时|并且|两项都',clause):
            share=float(positive[0][1])
            if 0<share<=300:
                question=f'同一设计同时要求光伏接入比例0%和{share:g}%，不能同时满足；请确认保留哪一项。'
                return dict(status='needs_clarification',issues=[question],questions=[question])
    # Only compare exact totals when the user explicitly binds them to the SAME
    # operating point; different operating points/ranges are not contradictions.
    if re.search(r'同一(?:个)?(?:基准)?工况|同一基准',text) and not re.search(r'范围|区间|至|不低于|不超过',text):
        totals=re.findall(r'(?:总负荷|总有功|总有功负荷)\s*(?:严格)?\s*(?:等于|为|是|=)?\s*(\d+(?:\.\d+)?)\s*(kW|MW)\b',text,re.I)
        simultaneous=re.search(r'(?:总负荷|总有功负荷)\s*(?:必须)?同时(?:严格)?等于\s*(\d+(?:\.\d+)?)\s*(kW|MW)\s*(?:和|与|及)\s*(\d+(?:\.\d+)?)\s*(kW|MW)\b',text,re.I)
        if simultaneous:
            a,unit_a,b,unit_b=simultaneous.groups()
            totals.extend([(a,unit_a),(b,unit_b)])
        values=sorted({round(float(n)*(1000 if unit.lower()=='mw' else 1),8) for n,unit in totals})
        if len(values)>1:
            question='同一基准工况总有功要求冲突：'+ '、'.join(f'{v:g}kW' for v in values)+'不能同时成立。请确认保留哪一项，或是否指不同工况。'
            return dict(status='needs_clarification',issues=[question],questions=[question])
    return None
