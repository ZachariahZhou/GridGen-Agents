"""Local single-user interface. Run: streamlit run app.py"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'src'))

import streamlit as st
import streamlit.components.v1 as components

from feeder_agents.agent import agent_session
from feeder_agents.reporting import verified_answer
from feeder_agents.rules import load_rules, search_sources
from feeder_agents.schemas import ExperimentSpec
from feeder_agents.workflow import run_experiment
from feeder_agents.ui_diagnostics import redact_text
from feeder_agents.ui_saved_records import build_verified_feeder_zip, load_json_record, load_verified_design_record, load_verified_experiment_record

st.set_page_config(page_title='Feeder Agents', page_icon='⚡', layout='wide')
from feeder_agents.visual_theme import render_saved_view
st.markdown('''<style>.block-container{max-width:1480px;padding-top:2rem}h1,h2,h3{letter-spacing:.01em}div[data-testid="stTabs"] button{font-size:14px}div[data-testid="stMetric"]{border:1px solid #dce3eb;border-radius:6px;padding:12px}</style>''',unsafe_allow_html=True)
st.title('馈线与输电网科研算例 · Feeder Agents')
st.caption('LangChain Agent · 可追溯规则 · OpenDSS 仿真 · 可复现实验')
display_layout=st.sidebar.radio('网络绘图布局',['topology','geographic'],
    format_func=lambda mode:'拓扑示意（自动排布）' if mode=='topology' else '合成空间坐标',
    help='仅改变绘图位置，不改变模型的节点、线路长度、电气参数或开关状态。')
display_scope=st.sidebar.radio('配电图展示范围',['mv','full','lv'],
    format_func=lambda scope:{'mv':'中压馈线骨架','full':'完整中低压网络','lv':'单个低压台区'}[scope],
    help='中压主图按配变汇总低压用户。模型导出保留全部节点；单电压与输电模型仍显示全网。')

def display_saved_network(path,*,view_context):
    from feeder_agents.ui_saved_records import load_json_record
    transformer_id=None
    model_file=Path(path).parent/'feeder.json'
    if display_scope=='lv' and model_file.exists():
        model_record,model_warning=load_json_record(model_file)
        if model_warning:st.warning(redact_text(model_warning))
        identifiers=[t['id'] for t in (model_record or {}).get('transformers',[]) if isinstance(t,dict) and 'id' in t]
        if identifiers:
            transformer_id=st.selectbox('查看低压台区',identifiers,key='lv-view:'+view_context+':'+str(Path(path).resolve()))
    return render_saved_view(path,layout_mode=display_layout,scope=display_scope,transformer_id=transformer_id)
workspace = Path('workspace').resolve()
design_tab, generate_tab, reference_tab, agent_tab, results_tab, rules_tab = st.tabs(['自然语言设计', '生成算例', 'MATPOWER参考馈线', '对话与知识工具', '实验结果', '设计规则与来源'])

with design_tab:
    import re
    from feeder_agents.design import design_from_request
    st.write('描述你需要的馈线或研究场景，系统将形成设计条件、列出假设，并生成模型。支持城乡逐相不平衡馈线、中压—配变—低压—用户的多电压模型，以及110–750kV单/多电压平衡输电网（MATPOWER）。交付网络、设备与负荷配置和模型文件；不生成负荷/光伏时序数据。潮流仅用于内部电气校验。')
    with st.form('natural_design'):
        design_project=st.text_input('设计项目 ID','default')
        st.session_state.setdefault('natural_design_id',datetime.now().strftime('design_%Y%m%d_%H%M%S'))
        design_id=st.text_input('设计 ID',key='natural_design_id')
        design_request=st.text_area('自然语言设计需求',placeholder='为光伏接入研究设计一条农村10kV馈线，12个负荷点，总峰值600kW，光伏容量为峰值负荷的50%。其余采用研究默认条件。')
        optional_nodes=st.text_input('总节点数（可选，包含1个电源节点）',placeholder='留空由系统确定，例如31')
        local_topology=st.checkbox('允许局部拓扑调整（同配变邻近分支用户重接）',value=False,help='保持用户位置、负荷、相别和电压层级。文字中明确要求保留拓扑时不启用。')
        draft_only=st.checkbox('仅形成设计草案，暂不运行潮流',value=False)
        design_submit=st.form_submit_button('根据需求设计馈线',type='primary')
    if design_submit:
        try:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',design_project):
                raise ValueError('无效项目 ID')
            with st.spinner('理解需求、形成设计方案并按电气反馈执行…'):
                designed=design_from_request(design_request,workspace/'projects'/design_project,design_id,execute=not draft_only,node_count=int(optional_nodes.strip()) if optional_nodes.strip() else None,local_topology=local_topology,normalize_requirements=True,planning_mode='adaptive')
            new_record=workspace/'projects'/design_project/'designs'/design_id/'result.json'
            if new_record.is_file():
                st.session_state['selected_design_record']=str(new_record)
                fresh_outcome=designed.get('outcome') if isinstance(designed,dict) else None
                summary_path=Path(fresh_outcome.get('directory',''))/'summary.json' if isinstance(fresh_outcome,dict) else Path('')
                if summary_path.is_file():
                    st.session_state['selected_experiment_summary']=str(summary_path.resolve())
            else:
                st.write(designed['verified_report'])
        except Exception as exc:
            from feeder_agents.ui_diagnostics import record_failure
            error_id=record_failure(workspace,exc,design_request,design_id)
            st.error(f'设计未完成：{type(exc).__name__}。'+(redact_text(str(exc)) if isinstance(exc,ValueError) else '系统执行异常，已保存诊断记录。')+f' 错误编号：{error_id}')
    st.button('刷新设计列表',key='refresh_design_records')
    design_records=[str(p) for p in sorted(workspace.glob('projects/*/designs/*/result.json'),
                                          key=lambda p:(p.stat().st_mtime_ns,str(p)),reverse=True)]
    if design_records:
        if st.session_state.get('selected_design_record') not in design_records:
            st.session_state['selected_design_record']=design_records[0]
        selected_design=Path(st.selectbox('查看设计记录',design_records,key='selected_design_record',
                            format_func=lambda p:str(Path(p).parent.relative_to(workspace))))
        designed,design_warning=load_verified_design_record(selected_design,workspace)
        if design_warning:st.warning(redact_text(design_warning))
        designed=designed or {'verified_report':'设计结果未验证，无法展示执行报告或模型视图。','outcome':None}
        st.write(designed.get('verified_report','设计记录缺少报告。'))
        if designed.get('outcome') is not None or design_warning is None:
            with st.expander('设计条件、来源与假设'):
                brief,brief_warning=load_json_record(selected_design.parent/'brief.json')
                if brief_warning:st.warning(redact_text(brief_warning))
                else:st.json(brief)
        outcome=designed.get('outcome')
        if outcome is not None and (not isinstance(outcome,dict) or not isinstance(outcome.get('directory'),str)):
            st.warning(f'{selected_design}: saved outcome has an invalid directory; model views were skipped.')
            outcome=None
        if outcome:
            output_path=Path(outcome['directory']).resolve()
            if output_path.is_relative_to(workspace):
                if designed.get('verified_family')=='feeder':
                    try:
                        bundle=build_verified_feeder_zip(outcome)
                    except (OSError,ValueError,KeyError,TypeError) as exc:
                        st.warning(redact_text(f'无法重建已验证设计模型包：{exc}'))
                    else:
                        st.download_button('下载设计模型包',bundle,file_name=f'{output_path.name}.zip')
                else:
                    archive=output_path/'dataset.zip'
                    if archive.exists() and 'dataset.zip' in outcome.get('artifacts',{}):
                        with archive.open('rb') as stream:
                            st.download_button('下载设计模型包',stream,file_name=f'{output_path.name}.zip')
                sample_views={row['sample_id']:output_path/row['sample_id']/'visualization.html'
                              for row in outcome.get('samples',[]) if isinstance(row,dict) and
                              re.fullmatch(r'sample_[0-9]{5}',row.get('sample_id','')) and
                              (output_path/row['sample_id']/'visualization.html').is_file()}
                if sample_views:
                    view_sample=st.selectbox('查看设计样本',list(sample_views),key=f'design_sample:{selected_design}')
                    view=sample_views[view_sample]
                    st.caption(f'当前视图：{selected_design.parent.relative_to(workspace)} / {view_sample}')
                    components.html(display_saved_network(view,view_context='design_sample'),height=850,scrolling=True)
                winner=outcome.get('winner')
                if isinstance(winner,dict) and isinstance(winner.get('conditions'),list) and winner['conditions']:
                    condition=st.selectbox('查看反向设计工况',winner['conditions'],format_func=lambda c:c['name'],key=f'design_condition:{selected_design}')
                    st.json({'target_met':condition['target_met'],'metrics':condition.get('metrics',{}),'constraints':condition.get('constraints',[]),'error':condition.get('error')})
                    condition_view=(output_path/condition['relative_path']/'visualization.html').resolve()
                    if condition_view.is_relative_to(output_path) and condition_view.exists():
                        components.html(display_saved_network(condition_view,view_context='design_condition'),height=850,scrolling=True)

with generate_tab:
    with st.expander('拓扑风格能力目录与组合限制'):
        from feeder_agents.rules import data
        st.json(data('topology_styles.json'))
    st.write('按电压等级选择研究规则包；单电压馈线模型，6/10/20kV可选不平衡，设备参数为研究假设，不含变压器/中性线。')
    selected_voltage = st.selectbox('额定线电压 kV', [.38, 6, 10, 20, 35], index=2)
    voltage_defaults = ExperimentSpec(voltage_kv=selected_voltage)
    from feeder_agents.rule_system import describe_rule_system
    with st.expander('所选电压规则与能力覆盖'):
        st.json(describe_rule_system(voltage_defaults))
    placement = st.selectbox('负荷布点', ['all_nodes','reference_conditioned'] if selected_voltage in (6,10,20) else ['all_nodes'], help='reference_conditioned保留零负荷连接点，按参考拓扑条件采样。')
    occupancy_reference = st.selectbox('负荷占位参考', ['case141','case69']) if placement=='reference_conditioned' else None
    with st.form('generate'):
        left, right = st.columns(2)
        with left:
            total_nodes = st.number_input('总节点数（含电源和连接点；0为自动）', min_value=0, max_value=2001, value=0)
            count = st.number_input('生成尝试数', min_value=1, max_value=10000, value=3)
            minimum = st.number_input('最少负荷点', min_value=2, max_value=2000, value=20)
            maximum = st.number_input('最多负荷点', min_value=2, max_value=2000, value=40)
            kw_min = st.number_input('最小总峰值负荷 kW', min_value=1.0, max_value=50000.0, value=float(voltage_defaults.total_kw_min), key=f'kw_min_{selected_voltage}')
            kw_max = st.number_input('最大总峰值负荷 kW', min_value=1.0, max_value=50000.0, value=float(voltage_defaults.total_kw_max), key=f'kw_max_{selected_voltage}')
        with right:
            scenario_kind = st.selectbox('空间场景（合成研究假设）', ['urban', 'rural'])
            phase_mode = st.selectbox('相别模式', ['balanced','unbalanced'])
            scene_engineering = st.checkbox('使用场景电缆/架空研究参数',value=True)
            phase_weights = st.text_input('不平衡负荷 A/B/C 权重（和为1）','0.5,0.3,0.2')
            st.caption('单/两相末端按场景自动设置；精确支线数量与PV分相可通过自然语言入口设置。')
            scenario_layout = st.selectbox('布局方式', ['spatial_mst', 'structured_radial', 'rural_villages', 'empirical_tree', 'legacy_random'],
                help='empirical_tree使用EPRI实际馈线参考边际分布；rural_villages为未标定村落模板。')
            family=st.selectbox('结构族（仅structured_radial）',['long_trunk','comb','multi_branch','balanced_tree','irregular_tree','open_ring'])
            branch_count=st.number_input('支线/臂数（仅comb/multi_branch）',min_value=2,max_value=12,value=3)
            branching_factor=st.number_input('最大子分支数（仅两类tree）',min_value=2,max_value=4,value=2)
            comb_trunk=st.slider('comb主干节点比例',.15,.8,.4)
            tie_count=st.number_input('常开联络数量（open_ring需1）',min_value=0,max_value=10,value=0)
            load_shape=st.selectbox('负荷空间分布',['heterogeneous','uniform','downstream_heavy','upstream_heavy'])
            concentration=st.slider('定向负荷加权强度（仅末端/近源偏重）',0.,4.,2.)
            pv = st.slider('PV容量 / 峰值负荷', 0.0, 3.0, 0.3)
            pf = st.slider('负荷功率因数', 0.5, 1.0, 0.95)
            seed = st.number_input('随机种子', min_value=0, max_value=2**32-1, value=42)
            repair_strategy = st.selectbox('修复策略', ['fixed', 'none', 'heuristic', 'agent'], help='agent会调用.env模型；heuristic为离线程序策略。')
            allow_rewire = st.checkbox('允许修复时重接支路', value=False)
            mode = st.selectbox('验收模式', ['normal', 'stress'],
                                help='stress 保留有效模型的运行越限，不保证制造某种特定越限。')
            workers = st.number_input('并行进程', min_value=1, max_value=16, value=1)
        st.session_state.setdefault('generation_run_id',datetime.now().strftime('run_%Y%m%d_%H%M%S'))
        run_id = st.text_input('实验 ID（相同配置可恢复）',key='generation_run_id')
        submit = st.form_submit_button('生成并验证', type='primary')
    if submit:
        try:
            topology={'family':family}
            if family in {'comb','multi_branch'}:topology['branch_count']=branch_count
            if family in {'balanced_tree','irregular_tree'}:topology['branching_factor']=branching_factor
            if family=='comb':topology['trunk_fraction']=comb_trunk
            spec = ExperimentSpec(voltage_kv=selected_voltage, n_buses=total_nodes or None, count=count, n_loads_min=minimum, n_loads_max=maximum,
                                  total_kw_min=kw_min, total_kw_max=kw_max, pv_ratio=pv,
                                  power_factor=pf, seed=seed, mode=mode, phase_design=({'mode':phase_mode,'load_phase_weights':[float(x) for x in phase_weights.split(',')]} if phase_mode=='unbalanced' else {}), scenario={'engineering_profile':scenario_kind if scene_engineering else 'generic','kind': scenario_kind,'layout':scenario_layout,'topology':topology if scenario_layout=='structured_radial' else None,
                                      'tie_count':tie_count,'load_shape':load_shape,
                                      'load_concentration':concentration if load_shape in {'downstream_heavy','upstream_heavy'} else 2,
                                      'load_placement':placement, 'reference_case_id':occupancy_reference,
                                      'calibration_profile':'epri_dpv_j1_k1' if scenario_layout=='empirical_tree' else None},
                                  repair_policy={'strategy': repair_strategy, 'allow_rewire': allow_rewire})
            with st.spinner('生成、仿真并写入实验包…'):
                result = run_experiment(spec, workspace, run_id, workers)
            st.session_state['selected_experiment_summary']=str(workspace/'experiments'/run_id/'summary.json')
            st.success(f'完成 {result["attempted"]} 次尝试，接受 {result["accepted"]} 条。请在实验结果中查看。')
        except Exception as exc:
            st.error(redact_text(str(exc)))

with agent_tab:
    st.write('自动读取项目启动目录的 .env：FEEDER_MODEL、OPENAI_API_KEY、OPENAI_BASE_URL；显式环境变量优先。会话和项目记忆保存在本地 SQLite。')
    project = st.text_input('项目 ID', 'default')
    thread = st.text_input('会话 ID', 'default')
    prompt = st.text_area('研究需求', placeholder='生成3条10kV馈线，每条30个负荷点，总峰值2MW，光伏容量占峰值负荷30%。')
    if st.button('交给 Agent', type='primary'):
        try:
            with st.spinner('Agent 正在查询规则并调用工具…'):
                with agent_session(workspace, project) as agent:
                    result = agent.invoke({'messages': [{'role': 'user', 'content': prompt}]},
                                          {'configurable': {'thread_id': thread}, 'recursion_limit': 64})
            st.write(verified_answer(result['messages']))
            with st.expander('工具与会话记录'):
                for message in result['messages']:
                    st.text(f'{message.type}: {message.content}')
        except Exception as exc:
            st.error(redact_text(str(exc)))

with results_tab:
    manifests = [str(p) for p in sorted([*workspace.glob('experiments/*/summary.json'),*workspace.glob('projects/*/experiments/*/summary.json')],
                                        key=lambda p:(p.stat().st_mtime_ns,str(p)),reverse=True)]
    if not manifests:
        st.info('暂无实验，请先生成算例。')
    else:
        if st.session_state.get('selected_experiment_summary') not in manifests:
            st.session_state['selected_experiment_summary']=manifests[0]
        selected = Path(st.selectbox('选择实验', manifests, key='selected_experiment_summary',
                                    format_func=lambda p: str(Path(p).parent.relative_to(workspace))))
        summary,summary_warning = load_verified_experiment_record(selected)
        if summary_warning:st.warning(redact_text(summary_warning))
        if summary is not None:
            a, b, c = st.columns(3)
            a.metric('尝试数', summary['attempted'])
            b.metric('接受数', summary['accepted'])
            c.metric('运行检查通过', summary['operational_pass'])
            st.dataframe([{k: v for k, v in s.items() if k != 'artifacts'} for s in summary['samples']], width='stretch')
            try:
                bundle=build_verified_feeder_zip(summary)
            except (OSError,ValueError,KeyError,TypeError) as exc:
                st.warning(redact_text(f'无法重建已验证实验包：{exc}'))
            else:
                st.download_button('下载完整实验 ZIP',bundle,file_name=f'{selected.parent.name}.zip')
            sample_id = st.selectbox('选择样本', [s['sample_id'] for s in summary['samples']],key=f'experiment_sample:{selected}')
            directory = selected.parent / sample_id
            st.caption(f'当前实验视图：{selected.parent.relative_to(workspace)} / {sample_id}')
            view = directory / 'visualization.html'
            if view.exists():
                components.html(display_saved_network(view,view_context='experiment'), height=850, scrolling=True)
            validation = directory / 'validation.json'
            if validation.exists():
                with st.expander('规则验证证据'):
                    validation_record,validation_warning=load_json_record(validation)
                    if validation_warning:st.warning(redact_text(validation_warning))
                    else:st.json(validation_record)
            st.caption('接受只代表所选科研模式下通过已实现检查，不代表完整设计合规或真实网络统计验证。')
            with st.expander('反馈修改此案例（保存独立版本）'):
                st.caption('支持局部修改，或明确要求重新设计空间MST/农村村落/长链/主干多支线/多臂/分层树/不规则树/常开环布局。重设计保留节点数量、总负荷、总PV和电压；连接关系、各节点分配和导线可能变化。请写清需要冻结的条件。')
                feedback=st.text_area('工程师反馈',placeholder='将所有线路长度及坐标距离放大1.2倍，保持负荷、PV、导线和拓扑不变。')
                revision_id=st.text_input('新修订 ID',datetime.now().strftime('revision_%Y%m%d_%H%M%S'))
                draft_revision=st.checkbox('先查看修订计划',value=True)
                if st.button('提交案例反馈'):
                    try:
                        from feeder_agents.revisions import revise_case
                        with st.spinner('解释反馈并核对不变项…'):
                            revised=revise_case(selected.parent.parent.parent,selected.parent.name,revision_id,
                                int(sample_id.split('_')[-1]),feedback=feedback,execute=not draft_revision)
                        st.write(revised['verified_report'])
                        st.json({k:revised[k] for k in ('plan','operation','diff','style_target_checks','directory') if k in revised})
                    except Exception as exc:
                        st.error(redact_text(str(exc)) if isinstance(exc,ValueError) else f'修订失败：{type(exc).__name__}；请检查模型配置和记录。')


with rules_tab:
    query = st.text_input('检索来源（如 电压、5729、SMART）')
    for source in search_sources(query):
        st.markdown(f'**{source["title"]}** · {source["access"]}')
        st.write(source['summary'])
        st.write(f'版本：{source["version"]}；定位：{source["locator"]}')
        if source['url'].startswith('https://'):
            st.link_button('查看原始来源', source['url'])
    with st.expander('全部可执行规则'):
        st.json(load_rules())

# Knowledge management shares the selected project's storage with its Agent.
with st.expander('项目知识库：导入设计条款与提取规则'):
    import re
    from feeder_agents.knowledge import DocumentStore, extract_rules
    knowledge_project = st.text_input('知识库项目 ID（与自然语言 Agent 一致）', 'default')
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',knowledge_project):
        store = DocumentStore(workspace/'projects'/knowledge_project)
        document_title = st.text_input('文档标题', '配网研究设计条件')
        upload = st.file_uploader('导入文本或 Markdown（暂不支持 PDF/OCR）',type=['txt','md'])
        document_text = st.text_area('或粘贴完整条款（包含适用条件和例外）')
        if st.button('保存设计文档'):
            try:
                text = upload.getvalue().decode('utf-8') if upload else document_text
                st.json(store.import_text(document_title,text))
            except Exception as exc:
                st.error(redact_text(str(exc)))
        snippets = store.search()
        if snippets:
            selected_chunk = st.selectbox('选择要提取的文档片段',range(len(snippets)),
                format_func=lambda i:f"{snippets[i]['title']} · 片段 {snippets[i]['chunk_index']+1}")
            snippet=snippets[selected_chunk]
            st.caption('一次提取一个片段；引用其他条款、条件不完整或不支持的内容不会自动转成规则。')
            st.code(snippet['text'],language=None)
            if st.button('使用模型提取候选规则'):
                try:
                    with st.spinner('提取原文依据、数值约束与适用范围…'):
                        extracted=extract_rules(store,snippet['document_id'],snippet['chunk_index'])
                    st.json(extracted)
                    st.info('将规则 ID 告诉同项目的 Agent，要求在实验计划中使用。候选解释不代表法规认证。')
                except Exception as exc:
                    st.error(f'提取未完成：{type(exc).__name__}；请检查模型配置和原文。')
        rule_page = store.list_rules()
        if rule_page['rules']:
            st.write(f"已保存 {rule_page['total']} 条候选规则（显示前32条），可以直接复用 ID：")
            st.dataframe([{k:r[k] for k in ('rule_id','source_title','locator','metric','operator','threshold','unit')}
                          for r in rule_page['rules']],width='stretch')
    else:
        st.error('项目 ID 只能包含字母、数字、下划线或连字符。')

with st.expander('受控参数扫描：同一基础网络的 PV / 负荷对比'):
    from feeder_agents.studies import build_study_plan, run_study
    with st.form('paired_study'):
        study_project = st.text_input('研究项目 ID','default')
        study_id = st.text_input('研究 ID',datetime.now().strftime('study_%Y%m%d_%H%M%S'))
        study_count = st.number_input('基础网络数量',min_value=1,max_value=1000,value=1)
        study_loads = st.number_input('每条网络的负荷点数',min_value=2,max_value=2000,value=20)
        study_kw = st.number_input('基准总负荷 kW',min_value=1.0,max_value=50000.0,value=1000.0)
        study_kind = st.selectbox('研究空间场景',['urban','rural'])
        pv_levels = st.text_input('PV容量 / 基准负荷（逗号分隔）','0,0.5,1.0')
        load_levels = st.text_input('负荷倍数（逗号分隔）','1.0')
        st.caption('固定网络、设备和合同容量；负荷变化时PV容量保持不变。扫描期间不进行修复，越限结果会保留。')
        submit_study = st.form_submit_button('生成受控研究')
    if submit_study:
        try:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',study_project):
                raise ValueError('无效项目 ID')
            plan=build_study_plan('PV与负荷水平对馈线运行的影响',{
                'count':study_count,'n_loads_min':study_loads,'n_loads_max':study_loads,
                'total_kw_min':study_kw,'total_kw_max':study_kw,'scenario':{'kind':study_kind}},
                [float(x.strip()) for x in pv_levels.split(',')],
                [float(x.strip()) for x in load_levels.split(',')])
            with st.spinner('冻结基础网络并计算各配对工况…'):
                study_result=run_study(plan,workspace/'projects'/study_project,study_id)
            st.success(study_result['verified_report'])
        except Exception as exc:
            st.error(redact_text(str(exc)))
    study_files=sorted(workspace.glob('studies/*/summary.json'))+sorted(workspace.glob('projects/*/studies/*/summary.json'))
    if study_files:
        chosen_study=st.selectbox('查看受控研究',study_files,format_func=lambda p:str(p.parent.relative_to(workspace)))
        study_result,study_warning=load_json_record(chosen_study)
        if study_warning:st.warning(redact_text(study_warning))
        if study_result is not None and study_result.get('samples'):
            st.dataframe(study_result.get('combinations',[]),width='stretch')
            csv_path=chosen_study.parent/'comparison.csv'
            if csv_path.exists():
                st.download_button('下载逐案例指标 CSV',csv_path.read_bytes(),file_name=f'{chosen_study.parent.name}.csv')
            zip_path=chosen_study.parent/'dataset.zip'
            if zip_path.exists():
                with zip_path.open('rb') as stream:
                    st.download_button('下载研究全部模型 ZIP',stream,file_name=f'{chosen_study.parent.name}.zip')
            chosen_case=st.selectbox('比较工况',range(len(study_result['samples'])),format_func=lambda i:
                f"{study_result['samples'][i]['base_case_id']} · PV={study_result['samples'][i]['pv_capacity_ratio']} · 负荷倍数={study_result['samples'][i]['load_scale']}")
            view=chosen_study.parent/study_result['samples'][chosen_case]['relative_path']/'visualization.html'
            if view.exists():
                components.html(display_saved_network(view,view_context='study'),height=850,scrolling=True)

with reference_tab:
    from feeder_agents.references import list_references,run_reference_case
    st.write('保留原电压、线路阻抗、拓扑和零负荷连接点。文献基准不等于真实测量；示意图不是地理布局。')
    evidence = st.selectbox('来源类别', ['any','actual_derived','benchmark'])
    reference_rows = list_references(evidence_class=evidence)
    chosen = st.selectbox('参考馈线', [r['case_id'] for r in reference_rows])
    if chosen:
        selected = next(r for r in reference_rows if r['case_id']==chosen)
        st.json(selected)
        scale = st.number_input('总负荷倍率（P、Q同比缩放）', min_value=.05, max_value=5.0, value=1.0)
        reference_id = st.text_input('参考实验 ID', value=datetime.now().strftime('reference_%Y%m%d_%H%M%S'))
        if st.button('生成参考模型与验证', disabled=not selected['statistics']['export_supported']):
            try:
                with st.spinner('导出并运行参考馈线…'):
                    result = run_reference_case(chosen,workspace,reference_id,scale)
                st.write(result['verified_report'])
                directory=Path(result['directory'])
                components.html(display_saved_network(directory/'visualization.html',view_context='reference'),height=850,scrolling=True)
                st.download_button('下载MATPOWER/OpenDSS模型包',(directory/'dataset.zip').read_bytes(),file_name=reference_id+'.zip')
            except Exception as exc:
                st.error(redact_text(str(exc)))
