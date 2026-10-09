"""Conservative checks for explicit deliverables and simultaneous hard constraints.

This is a guard for unambiguous expressions, not a general natural-language parser.
Unmatched wording still goes through the evidence-linked LLM compiler.
"""
import re

PRODUCT_SCOPE = (
    'Transmission and distribution models aim for engineering plausibility: consistent requirements, compatible equipment types and voltages, consistent parameter units and relationships, and compliance with the electrical constraints specified by the task. Real feeder distributions and equipment catalogs are references; reproducing real networks is not required. Do not treat missing calibration to real distributions as a blocker unless the user requests statistical fitting. '
    'This system delivers research grid models (distribution feeders or single/multi-voltage balanced transmission networks): topology, spatial positions, phases, equipment and customer-load configurations, visualizations, and callable model files. '
    'It does not generate 8760-hour, daily/annual load, or PV time-series data, and does not deliver snapshots as generation products; power flow is used only for internal electrical validation. '
    'Users may provide their own time series or use feeders for subsequent time-series research; do not reject such network-model requests. '
    'Electromagnetic-transient, protection-operation, and fault-waveform models are not delivered; using static networks for subsequent user-led transient research is allowed. '
    'Three-phase LV uses an equivalent grounded model without explicit neutral conductors, neutral potentials, or neutral displacement; do not promise a four-conductor mutual-impedance model. '
    'Current generation tools model and convert parameters at 50Hz. Do not silently substitute 50Hz for another explicitly requested frequency. '
)

INTENT_GUIDANCE = PRODUCT_SCOPE + (
    'Ordinary multi-voltage feedback supports authorized local topology adjustments: reconnect LV customers to neighboring branches within the same transformer service area while fixing node positions, loads, phases, voltage levels, and installation methods. The execution layer reads this permission from the original request; do not create extra assignment fields for it. Explicit topology preservation prohibits rewiring. This is not cross-transformer reassignment, arbitrary topology redesign, or GIS planning. '
    'Check every explicit requirement before deciding whether to generate. If time-series generation is requested, explain the delivery scope; do not silently substitute a network model for the entire request. '
    'Conflicting hard constraints on the same baseline must appear in blocking_questions with both conflicting values and units in full, asking which to retain. Do not average, override, or independently select either value. '
    'Identify both sides of conflicts between no PV and a positive PV ratio, universal duct installation with no exceptions and regional overhead installation, or exclusion of all installation methods. '
    'unsupported must explain the specific capability boundary, rather than merely restating a supported requirement. 50% PV and no PV are each supported individually. '
    'assignments may be empty for refusal or clarification; equipment/installation analysis need not be added. '
    'lv_installation_decision and lv_regions must be inside assignment values, not top-level intent fields. '
    'For example, {"field":"hierarchy.lv_installation_decision","value":{"selected":"aerial_bundle","reason":"Research assumption","factors":[],"alternatives":[],"unknowns":[]},"origin":"inferred","reason":"Equipment selection"} illustrates nesting only; actual alternatives must be complete. '
)


def request_guard(text):
    # Exact count intervals can be checked arithmetically without asking an LLM
    # to construct conflicting executable specifications. Do not reinterpret
    # revisions, negations, examples or constraints on different quantities.
    for clause in re.split(r'[。；;\n]',text):
        if re.search('\u4e0d\u8981\u6c42|\u4e0d\u9700\u8981|\u65e0\u9700|\u539f\u5148|\u6b64\u524d|\u539f\u6765|\u6539\u4e3a|\u4f8b\u5982|\u4e3e\u4f8b',clause):continue
        interval=re.search('((?:\u7528\u6237|\u8282\u70b9|\u6bcd\u7ebf)(?:\u6570\u91cf|\u603b\u6570|\u6570))\\s*(?:\u5fc5\u987b)?\\s*(?:\u81f3\u5c11|\u4e0d\u5c11\u4e8e|\u4e0d\u4f4e\u4e8e)\\s*(\\d+)\\s*(?:\u4e2a)?\\s*(?:\u4e14|\u5e76\u4e14|\u540c\u65f6|\u548c)\\s*(?:\u81f3\u591a|\u4e0d\u8d85\u8fc7|\u6700\u591a)\\s*(\\d+)',clause)
        if interval and int(interval[2])>int(interval[3]):
            issue=f'The lower bound {interval[2]} for {interval[1]} exceeds the upper bound {interval[3]}; both limits cannot hold simultaneously. Please clarify the count range to retain.'
            return dict(status='needs_clarification',issues=[issue],questions=[issue])
    # Inspect clauses separately: an external-data statement must not hide a
    # separate explicit demand for generated time series.
    for clause in re.split(r'[，,。；;\n]',text):
        frequency=re.search('(?:\u751f\u6210|\u6784\u5efa|\u5efa\u7acb|\u4ea4\u4ed8)\\s*(\\d+(?:\\.\\d+)?)\\s*(?:Hz|\u8d6b\u5179)|(?:\u9891\u7387\\s*(?:\u5fc5\u987b|\u8981\u6c42)?\\s*(?:\u4e3a|\u662f|=|\uff1a|:)?|\u5fc5\u987b\\s*(?:\u4e3a|\u4f7f\u7528|\u7528))\\s*(\\d+(?:\\.\\d+)?)\\s*(?:Hz|\u8d6b\u5179)',clause,re.I)
        if frequency and not re.search('\u4e0d\u9700\u8981|\u65e0\u9700|\u4e0d\u8981\u6c42|\u4e0d\u8981|\u81ea\u884c|\u540e\u7eed|\u539f\u5148|\u6539\u4e3a',clause):
            hz=float(next(v for v in frequency.groups() if v is not None))
            if hz!=50:return dict(status='unsupported',issues=[f'The request explicitly requires {hz:g}Hz, but current generation tools support only 50Hz; the requested frequency cannot be silently changed.'],questions=[])
        neutral=re.search('\u4e2d\u6027\u7ebf(?:\u7535\u4f4d|\u4f4d\u79fb)|neutral (?:potential|displacement)',clause,re.I)
        if neutral and re.search('\u9700\u8981|\u8981\u6c42|\u5fc5\u987b|\u663e\u5f0f|\u4ea4\u4ed8|require|explicit',clause,re.I) and not re.search('\u4e0d\u9700\u8981|\u65e0\u9700|\u4e0d\u8981\u6c42|\u4e0d\u8981|\u81ea\u884c|\u540e\u7eed|\u7528\u4e8e|do not|no need',clause,re.I):
            return dict(status='unsupported',issues=['Current three-phase LV tools use an equivalent grounded model and do not support explicit neutral potential or displacement. An equivalent model cannot substitute for the requested four-conductor model.'],questions=[])
        transient=re.search('\u6682\u6001|\u4fdd\u62a4\u52a8\u4f5c.*\u6ce2\u5f62|\u6545\u969c.*\u6ce2\u5f62|electromagnetic transient',clause,re.I)
        if transient and re.search('\u751f\u6210|\u8f93\u51fa|\u4ea4\u4ed8|\u63d0\u4f9b|\u6a21\u62df|generate|deliver|simulate',clause,re.I) and not re.search('\u4e0d\u9700\u8981|\u65e0\u9700|\u4e0d\u751f\u6210|\u4e0d\u8981\u6c42|\u4e0d\u8981|\u81ea\u884c|\u81ea\u5907|\u540e\u7eed|\u7528\u4e8e|do not|no need|my own|provided by',clause,re.I):
            return dict(status='unsupported',issues=['This system delivers static research grid models, not transient/protection-operation simulations or fault waveforms. This explicit deliverable exceeds its capabilities.'],questions=[])
        temporal=re.search('8760|\u9010\u5c0f\u65f6|\u65f6\u5e8f|time[ -]?series|hourly',clause,re.I)
        if not temporal:continue
        external=re.search('\u4e0d\u9700\u8981|\u65e0\u9700|\u4e0d\u751f\u6210|\u4e0d\u8981\u6c42|\u4e0d\u8981|\u81ea\u884c|\u81ea\u5907|\u81ea\u5df1\u63d0\u4f9b|\u7531(?:\u6211|\u7528\u6237|\u6211\u4eec)\u63d0\u4f9b|\u6211(?:\u6765)?\u63d0\u4f9b|\u540e\u7eed|\u7528\u4e8e|do not|no need|my own|provided by',clause,re.I)
        demand=re.search('\u751f\u6210|\u8f93\u51fa|\u4ea4\u4ed8|\u63d0\u4f9b|\u8981\u6c42|\u9700\u8981|\u9644\u5e26|generate|produce|deliver|supply',clause,re.I)
        if demand and not external:
            return dict(status='unsupported',issues=[PRODUCT_SCOPE+' This request explicitly requires generating time-series data, which is outside the delivery scope.'],questions=[])
    # A simultaneous 0% versus positive PV share is a clarification, since
    # either request is supported alone. Require complete comma-delimited
    # requirement atoms; a negated/example prefix cannot become a hard value.
    for clause in re.split(r'[。；;\n]',text):
        if re.search('\u65e7\u65b9\u6848|\u53c2\u8003\u6587\u732e|\u539f\u5148|\u6b64\u524d|\u539f\u6765|\u6539\u4e3a|\u4fee\u8ba2|\u66f4\u65b0\u4e3a|\u5e76\u975e|\u4e0d\u662f|\u4e0d\u518d|\u65e0\u9700|\u4e0d\u8981\u6c42|\u53ef\u9009|\u53ef\u4ee5|\u6216\u8005|\u6216\u662f|\u4e0d\u540c\u5de5\u51b5|\u4e0d\u540c\u573a\u666f|\u4f8b\u5982|\u4e3e\u4f8b|\u793a\u4f8b|\u5047\u8bbe|\u5047\u5982|\u5982\u679c|\u82e5',clause):
            continue
        labels=re.findall('(?:\u5de5\u51b5|\u573a\u666f|\u65b9\u6848)\\s*([A-Za-z\u7532\u4e59\u4e00\u4e8c0-9]+)',clause)
        if len(set(labels))>1:
            continue
        atoms=[part.strip() for part in re.split(r'[，,]',clause) if part.strip()]
        zero=any(re.fullmatch('\u7981\u6b62\u4efb\u4f55\u5149\u4f0f\u63a5\u5165|\u4e0d\u63a5\u5165\u5149\u4f0f|\u65e0\u5149\u4f0f\u63a5\u5165',atom) for atom in atoms)
        positive=[match for atom in atoms if (match:=re.fullmatch(
            '(?:(?:\u540c\u65f6|\u5e76\u4e14)\\s*)?\u5fc5\u987b\u63a5\u5165\u5360\u603b\u8d1f\u8377\\s*(\\d+(?:\\.\\d+)?)\\s*%\\s*\u7684\u5149\u4f0f',atom))]
        if zero and len(positive)==1 and re.search('\u540c\u65f6|\u5e76\u4e14|\u4e24\u9879\u90fd',clause):
            share=float(positive[0][1])
            if 0<share<=300:
                question=f'The same design simultaneously requires PV connection ratios of 0% and {share:g}%; both cannot be satisfied. Please confirm which to retain.'
                return dict(status='needs_clarification',issues=[question],questions=[question])
    # Only compare exact totals when the user explicitly binds them to the SAME
    # operating point; different operating points/ranges are not contradictions.
    if re.search('\u540c\u4e00(?:\u4e2a)?(?:\u57fa\u51c6)?\u5de5\u51b5|\u540c\u4e00\u57fa\u51c6',text) and not re.search('\u8303\u56f4|\u533a\u95f4|\u81f3|\u4e0d\u4f4e\u4e8e|\u4e0d\u8d85\u8fc7',text):
        totals=re.findall('(?:\u603b\u8d1f\u8377|\u603b\u6709\u529f|\u603b\u6709\u529f\u8d1f\u8377)\\s*(?:\u4e25\u683c)?\\s*(?:\u7b49\u4e8e|\u4e3a|\u662f|=)?\\s*(\\d+(?:\\.\\d+)?)\\s*(kW|MW)\\b',text,re.I)
        simultaneous=re.search('(?:\u603b\u8d1f\u8377|\u603b\u6709\u529f\u8d1f\u8377)\\s*(?:\u5fc5\u987b)?\u540c\u65f6(?:\u4e25\u683c)?\u7b49\u4e8e\\s*(\\d+(?:\\.\\d+)?)\\s*(kW|MW)\\s*(?:\u548c|\u4e0e|\u53ca)\\s*(\\d+(?:\\.\\d+)?)\\s*(kW|MW)\\b',text,re.I)
        if simultaneous:
            a,unit_a,b,unit_b=simultaneous.groups()
            totals.extend([(a,unit_a),(b,unit_b)])
        values=sorted({round(float(n)*(1000 if unit.lower()=='mw' else 1),8) for n,unit in totals})
        if len(values)>1:
            question='Conflicting total active-power requirements for the same baseline condition: '+ ', '.join(f'{v:g}kW' for v in values)+' cannot hold simultaneously. Please confirm which to retain, or whether they refer to different operating conditions.'
            return dict(status='needs_clarification',issues=[question],questions=[question])
    return None
