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
st.title('GridGen-Agents: Distribution and Transmission Research Cases')
st.caption('LangChain agents | Traceable rules | OpenDSS simulation | Reproducible cases')
display_layout=st.sidebar.radio('Network layout',['topology','geographic'],
    format_func=lambda mode:'Topology (automatic layout)' if mode=='topology' else 'Synthetic spatial coordinates',
    help='Layout changes affect display coordinates only. Buses, line lengths, electrical parameters and switch states remain part of the model.')
display_scope=st.sidebar.radio('Distribution view',['mv','full','lv'],
    format_func=lambda scope:{'mv':'MV backbone','full':'Full MV/LV network','lv':'One LV service area'}[scope],
    help='The MV view aggregates customers by transformer. Exports retain all buses. Single-voltage and transmission views show the full network.')

def display_saved_network(path,*,view_context):
    from feeder_agents.ui_saved_records import load_json_record
    transformer_id=None
    model_file=Path(path).parent/'feeder.json'
    if display_scope=='lv' and model_file.exists():
        model_record,model_warning=load_json_record(model_file)
        if model_warning:st.warning(redact_text(model_warning))
        identifiers=[t['id'] for t in (model_record or {}).get('transformers',[]) if isinstance(t,dict) and 'id' in t]
        if identifiers:
            transformer_id=st.selectbox('Select LV service area',identifiers,key='lv-view:'+view_context+':'+str(Path(path).resolve()))
    return render_saved_view(path,layout_mode=display_layout,scope=display_scope,transformer_id=transformer_id)
workspace = Path('workspace').resolve()
design_tab, generate_tab, reference_tab, agent_tab, results_tab, rules_tab = st.tabs(['Natural-language design', 'Generate cases', 'MATPOWER reference feeders', 'Agent and knowledge tools', 'Results', 'Rules and sources'])

with design_tab:
    import re
    from feeder_agents.design import design_from_request
    st.write('Describe the network and research requirements. The system builds a specification, records assumptions and generates a model. Supported families include urban/rural unbalanced feeders, MV-transformer-LV-customer networks, and balanced 110-750 kV transmission networks. Outputs include topology, equipment, injections and callable model files. Power flow validates the generated network; load and PV time series are outside this workflow.')
    with st.form('natural_design'):
        design_project=st.text_input('Design project ID','default')
        st.session_state.setdefault('natural_design_id',datetime.now().strftime('design_%Y%m%d_%H%M%S'))
        design_id=st.text_input('Design ID',key='natural_design_id')
        design_request=st.text_area('Design request',placeholder='Design a rural 10 kV feeder for PV integration research, with 12 load points, 600 kW peak demand and PV capacity equal to 50% of peak demand. Use research defaults for unspecified settings.')
        optional_nodes=st.text_input('Total buses (optional, including one source)',placeholder='Leave blank for automatic sizing; for example, 31')
        local_topology=st.checkbox('Allow local topology repairs within a transformer service area',value=False,help='Preserve customer positions, loads, phases and voltage levels. Explicit requests to preserve topology take precedence.')
        draft_only=st.checkbox('Draft only (no electrical generation)',value=False)
        design_submit=st.form_submit_button('Generate from request',type='primary')
    if design_submit:
        try:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',design_project):
                raise ValueError('Invalid project ID')
            with st.spinner('Interpreting requirements, planning and checking electrical feedback...'):
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
            st.error(f'Design incomplete: {type(exc).__name__}. '+(redact_text(str(exc)) if isinstance(exc,ValueError) else 'Execution failed; a diagnostic record has been saved.')+f' Error ID: {error_id}')
    st.button('Refresh designs',key='refresh_design_records')
    design_records=[str(p) for p in sorted(workspace.glob('projects/*/designs/*/result.json'),
                                          key=lambda p:(p.stat().st_mtime_ns,str(p)),reverse=True)]
    if design_records:
        if st.session_state.get('selected_design_record') not in design_records:
            st.session_state['selected_design_record']=design_records[0]
        selected_design=Path(st.selectbox('Select design',design_records,key='selected_design_record',
                            format_func=lambda p:str(Path(p).parent.relative_to(workspace))))
        designed,design_warning=load_verified_design_record(selected_design,workspace)
        if design_warning:st.warning(redact_text(design_warning))
        designed=designed or {'verified_report':'The design result is unverified; a verified report and model view are unavailable.','outcome':None}
        st.write(designed.get('verified_report','The design record has no report.'))
        if designed.get('outcome') is not None or design_warning is None:
            with st.expander('Specification, evidence and assumptions'):
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
                        st.warning(redact_text(f'Could not rebuild the verified design archive: {exc}'))
                    else:
                        st.download_button('Download design archive',bundle,file_name=f'{output_path.name}.zip')
                else:
                    archive=output_path/'dataset.zip'
                    if archive.exists() and 'dataset.zip' in outcome.get('artifacts',{}):
                        with archive.open('rb') as stream:
                            st.download_button('Download design archive',stream,file_name=f'{output_path.name}.zip')
                sample_views={row['sample_id']:output_path/row['sample_id']/'visualization.html'
                              for row in outcome.get('samples',[]) if isinstance(row,dict) and
                              re.fullmatch(r'sample_[0-9]{5}',row.get('sample_id','')) and
                              (output_path/row['sample_id']/'visualization.html').is_file()}
                if sample_views:
                    view_sample=st.selectbox('Select design sample',list(sample_views),key=f'design_sample:{selected_design}')
                    view=sample_views[view_sample]
                    st.caption(f'Current view: {selected_design.parent.relative_to(workspace)} / {view_sample}')
                    components.html(display_saved_network(view,view_context='design_sample'),height=850,scrolling=True)
                if designed.get('verified_family') in ('electrical_response','research_task'):
                    st.json({k:outcome[k] for k in ('target_met','verification_passed','protected_contract_preserved','accepted_steps','stop_reason')})
                    if outcome.get('diagnosis'):
                        with st.expander('Response diagnosis and adjustment evidence',expanded=not outcome['target_met']):
                            st.write(outcome['diagnosis']['summary'])
                            st.json(outcome['diagnosis'])
                    st.dataframe(outcome['selected']['targets'])
                    if outcome.get('task_validation'):
                        with st.expander('Research-task suitability',expanded=True):
                            st.json(outcome['task_validation'])
                    response_view=output_path/'selected'/'visualization.html'
                    if response_view.is_file():
                        components.html(display_saved_network(response_view,view_context='electrical_response'),height=850,scrolling=True)
                winner=outcome.get('winner')
                if isinstance(winner,dict) and isinstance(winner.get('conditions'),list) and winner['conditions']:
                    condition=st.selectbox('Select inverse-design condition',winner['conditions'],format_func=lambda c:c['name'],key=f'design_condition:{selected_design}')
                    st.json({'target_met':condition['target_met'],'metrics':condition.get('metrics',{}),'constraints':condition.get('constraints',[]),'error':condition.get('error')})
                    condition_view=(output_path/condition['relative_path']/'visualization.html').resolve()
                    if condition_view.is_relative_to(output_path) and condition_view.exists():
                        components.html(display_saved_network(condition_view,view_context='design_condition'),height=850,scrolling=True)

with generate_tab:
    with st.expander('Topology styles and compatibility'):
        from feeder_agents.rules import data
        st.json(data('topology_styles.json'))
    st.write('Select a voltage-dependent research rule set. This form generates single-voltage feeders; 6/10/20 kV supports unbalanced operation. Equipment uses reference-derived catalogs and explicit engineering assumptions. Transformers and explicit neutrals require a different model scope.')
    selected_voltage = st.selectbox('Nominal line voltage (kV)', [.38, 6, 10, 20, 35], index=2)
    voltage_defaults = ExperimentSpec(voltage_kv=selected_voltage)
    from feeder_agents.rule_system import describe_rule_system
    with st.expander('Voltage rules and supported capabilities'):
        st.json(describe_rule_system(voltage_defaults))
    placement = st.selectbox('Load placement', ['all_nodes','reference_conditioned'] if selected_voltage in (6,10,20) else ['all_nodes'], help='reference_conditioned retains zero-load junctions and samples occupancy conditional on reference topology.')
    occupancy_reference = st.selectbox('Load occupancy reference', ['case141','case69']) if placement=='reference_conditioned' else None
    with st.form('generate'):
        left, right = st.columns(2)
        with left:
            total_nodes = st.number_input('Total buses (source and junctions included; 0 = auto)', min_value=0, max_value=2001, value=0)
            count = st.number_input('Generation attempts', min_value=1, max_value=10000, value=3)
            minimum = st.number_input('Minimum load points', min_value=2, max_value=2000, value=20)
            maximum = st.number_input('Maximum load points', min_value=2, max_value=2000, value=40)
            kw_min = st.number_input('Minimum peak demand (kW)', min_value=1.0, max_value=50000.0, value=float(voltage_defaults.total_kw_min), key=f'kw_min_{selected_voltage}')
            kw_max = st.number_input('Maximum peak demand (kW)', min_value=1.0, max_value=50000.0, value=float(voltage_defaults.total_kw_max), key=f'kw_max_{selected_voltage}')
        with right:
            scenario_kind = st.selectbox('Synthetic spatial scenario', ['urban', 'rural'])
            phase_mode = st.selectbox('Phase mode', ['balanced','unbalanced'])
            scene_engineering = st.checkbox('Use scenario-dependent cable and overhead parameters',value=True)
            phase_weights = st.text_input('Phase A/B/C load weights (sum = 1)','0.5,0.3,0.2')
            st.caption('Single- and two-phase laterals follow the scenario. Use the natural-language entry point to specify lateral counts or PV phase allocation.')
            scenario_layout = st.selectbox('Generation layout', ['spatial_mst', 'structured_radial', 'rural_villages', 'empirical_tree', 'legacy_random'],
                help='empirical_tree uses EPRI feeder reference marginals; rural_villages uses a synthetic village template without population calibration.')
            family=st.selectbox('Topology family (structured_radial only)',['long_trunk','comb','multi_branch','balanced_tree','irregular_tree','open_ring'])
            branch_count=st.number_input('Lateral / arm count (comb or multi_branch)',min_value=2,max_value=12,value=3)
            branching_factor=st.number_input('Maximum child branches (tree families)',min_value=2,max_value=4,value=2)
            comb_trunk=st.slider('Comb trunk bus fraction',.15,.8,.4)
            tie_count=st.number_input('Normally-open ties (open_ring requires 1)',min_value=0,max_value=10,value=0)
            load_shape=st.selectbox('Spatial load distribution',['heterogeneous','uniform','downstream_heavy','upstream_heavy'])
            concentration=st.slider('Directional load concentration',0.,4.,2.)
            pv = st.slider('PV capacity / peak demand', 0.0, 3.0, 0.3)
            pf = st.slider('Load power factor', 0.5, 1.0, 0.95)
            seed = st.number_input('Random seed', min_value=0, max_value=2**32-1, value=42)
            repair_strategy = st.selectbox('Repair strategy', ['fixed', 'none', 'heuristic', 'agent'], help='agent uses the model configured in .env; heuristic uses an offline policy.')
            allow_rewire = st.checkbox('Allow branch reconnection during repair', value=False)
            mode = st.selectbox('Acceptance mode', ['normal', 'stress'],
                                help='stress retains valid models with operating-limit violations; it does not guarantee a specific violation.')
            workers = st.number_input('Worker processes', min_value=1, max_value=16, value=1)
        st.session_state.setdefault('generation_run_id',datetime.now().strftime('run_%Y%m%d_%H%M%S'))
        run_id = st.text_input('Experiment ID (resume with identical settings)',key='generation_run_id')
        submit = st.form_submit_button('Generate and validate', type='primary')
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
            with st.spinner('Generating, solving and saving the experiment...'):
                result = run_experiment(spec, workspace, run_id, workers)
            st.session_state['selected_experiment_summary']=str(workspace/'experiments'/run_id/'summary.json')
            st.success(f'Completed {result["attempted"]} attempts; accepted {result["accepted"]}. Open Results to inspect the cases.')
        except Exception as exc:
            st.error(redact_text(str(exc)))

with agent_tab:
    st.write('Reads FEEDER_MODEL, OPENAI_API_KEY and OPENAI_BASE_URL from .env in the launch directory; environment variables take precedence. Conversations and project memory are stored in local SQLite databases.')
    project = st.text_input('Project ID', 'default')
    thread = st.text_input('Conversation ID', 'default')
    prompt = st.text_area('Research request', placeholder='Generate three 10 kV feeders, each with 30 load points, 2 MW peak demand and PV capacity equal to 30% of peak demand.')
    if st.button('Run agent', type='primary'):
        try:
            with st.spinner('The agent is checking rules and calling tools...'):
                with agent_session(workspace, project) as agent:
                    result = agent.invoke({'messages': [{'role': 'user', 'content': prompt}]},
                                          {'configurable': {'thread_id': thread}, 'recursion_limit': 64})
            st.write(verified_answer(result['messages']))
            with st.expander('Tool calls and conversation'):
                for message in result['messages']:
                    st.text(f'{message.type}: {message.content}')
        except Exception as exc:
            st.error(redact_text(str(exc)))

with results_tab:
    manifests = [str(p) for p in sorted([*workspace.glob('experiments/*/summary.json'),*workspace.glob('projects/*/experiments/*/summary.json')],
                                        key=lambda p:(p.stat().st_mtime_ns,str(p)),reverse=True)]
    if not manifests:
        st.info('No experiments yet. Generate a case to begin.')
    else:
        if st.session_state.get('selected_experiment_summary') not in manifests:
            st.session_state['selected_experiment_summary']=manifests[0]
        selected = Path(st.selectbox('Select experiment', manifests, key='selected_experiment_summary',
                                    format_func=lambda p: str(Path(p).parent.relative_to(workspace))))
        summary,summary_warning = load_verified_experiment_record(selected)
        if summary_warning:st.warning(redact_text(summary_warning))
        if summary is not None:
            a, b, c = st.columns(3)
            a.metric('Attempts', summary['attempted'])
            b.metric('Accepted', summary['accepted'])
            c.metric('Operating checks passed', summary['operational_pass'])
            st.dataframe([{k: v for k, v in s.items() if k != 'artifacts'} for s in summary['samples']], width='stretch')
            try:
                bundle=build_verified_feeder_zip(summary)
            except (OSError,ValueError,KeyError,TypeError) as exc:
                st.warning(redact_text(f'Could not rebuild the verified experiment archive: {exc}'))
            else:
                st.download_button('Download experiment ZIP',bundle,file_name=f'{selected.parent.name}.zip')
            sample_id = st.selectbox('Select sample', [s['sample_id'] for s in summary['samples']],key=f'experiment_sample:{selected}')
            directory = selected.parent / sample_id
            st.caption(f'Current experiment view: {selected.parent.relative_to(workspace)} / {sample_id}')
            view = directory / 'visualization.html'
            if view.exists():
                components.html(display_saved_network(view,view_context='experiment'), height=850, scrolling=True)
            validation = directory / 'validation.json'
            if validation.exists():
                with st.expander('Rule validation evidence'):
                    validation_record,validation_warning=load_json_record(validation)
                    if validation_warning:st.warning(redact_text(validation_warning))
                    else:st.json(validation_record)
            st.caption('Acceptance means the implemented checks passed for the selected research mode. It does not establish complete planning compliance or statistical fidelity to a real network population.')
            with st.expander('Revise this case (save a separate version)'):
                st.caption('Request local edits or an explicit topology redesign. Redesign preserves bus count, total demand, total PV and voltage. Connectivity, nodal allocation and conductors may change. State any additional conditions that must remain fixed.')
                feedback=st.text_area('Engineer feedback',placeholder='Scale all line lengths and spatial distances by 1.2, preserving loads, PV, conductors and topology.')
                revision_id=st.text_input('New revision ID',datetime.now().strftime('revision_%Y%m%d_%H%M%S'))
                draft_revision=st.checkbox('Preview the revision plan',value=True)
                if st.button('Apply feedback'):
                    try:
                        from feeder_agents.revisions import revise_case
                        with st.spinner('Interpreting feedback and checking invariants...'):
                            revised=revise_case(selected.parent.parent.parent,selected.parent.name,revision_id,
                                int(sample_id.split('_')[-1]),feedback=feedback,execute=not draft_revision)
                        st.write(revised['verified_report'])
                        st.json({k:revised[k] for k in ('plan','operation','diff','style_target_checks','directory') if k in revised})
                    except Exception as exc:
                        st.error(redact_text(str(exc)) if isinstance(exc,ValueError) else f'Revision failed: {type(exc).__name__}; check the model configuration and logs.')


with rules_tab:
    query = st.text_input('Search sources (for example, voltage, 5729 or SMART)')
    for source in search_sources(query):
        st.markdown(f'**{source["title"]}** · {source["access"]}')
        st.write(source['summary'])
        st.write(f'Version: {source["version"]}; location: {source["locator"]}')
        if source['url'].startswith('https://'):
            st.link_button('Open source document', source['url'])
    with st.expander('Executable rules'):
        st.json(load_rules())

# Knowledge management shares the selected project's storage with its Agent.
with st.expander('Project knowledge: import documents and extract rules'):
    import re
    from feeder_agents.knowledge import DocumentStore, extract_rules
    knowledge_project = st.text_input('Knowledge project ID (shared with the agent)', 'default')
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',knowledge_project):
        store = DocumentStore(workspace/'projects'/knowledge_project)
        document_title = st.text_input('Document title', 'Distribution research design requirements')
        upload = st.file_uploader('Import text or Markdown (PDF/OCR not supported)',type=['txt','md'])
        document_text = st.text_area('Or paste complete clauses, including applicability and exceptions')
        if st.button('Save design document'):
            try:
                text = upload.getvalue().decode('utf-8') if upload else document_text
                st.json(store.import_text(document_title,text))
            except Exception as exc:
                st.error(redact_text(str(exc)))
        snippets = store.search()
        if snippets:
            selected_chunk = st.selectbox('Select document chunk',range(len(snippets)),
                format_func=lambda i:f"{snippets[i]['title']} | Chunk {snippets[i]['chunk_index']+1}")
            snippet=snippets[selected_chunk]
            st.caption('Extract one chunk at a time. Incomplete conditions, unresolved cross-references and unsupported clauses are not automatically activated as rules.')
            st.code(snippet['text'],language=None)
            if st.button('Extract candidate rules with the model'):
                try:
                    with st.spinner('Extracting source evidence, numerical constraints and applicability...'):
                        extracted=extract_rules(store,snippet['document_id'],snippet['chunk_index'])
                    st.json(extracted)
                    st.info('Provide a rule ID to the agent in the same project to include it in a plan. Candidate interpretations are not compliance certifications.')
                except Exception as exc:
                    st.error(f'Extraction incomplete: {type(exc).__name__}; check the model configuration and source text.')
        rule_page = store.list_rules()
        if rule_page['rules']:
            st.write(f"Saved {rule_page['total']} candidate rules (showing the first 32). Reuse their IDs:")
            st.dataframe([{k:r[k] for k in ('rule_id','source_title','locator','metric','operator','threshold','unit')}
                          for r in rule_page['rules']],width='stretch')
    else:
        st.error('Project IDs may contain letters, digits, underscores and hyphens only.')

with st.expander('Controlled PV / load sweep on fixed networks'):
    from feeder_agents.studies import build_study_plan, run_study
    with st.form('paired_study'):
        study_project = st.text_input('Study project ID','default')
        study_id = st.text_input('Study ID',datetime.now().strftime('study_%Y%m%d_%H%M%S'))
        study_count = st.number_input('Base network count',min_value=1,max_value=1000,value=1)
        study_loads = st.number_input('Load points per network',min_value=2,max_value=2000,value=20)
        study_kw = st.number_input('Baseline demand (kW)',min_value=1.0,max_value=50000.0,value=1000.0)
        study_kind = st.selectbox('Study scenario',['urban','rural'])
        pv_levels = st.text_input('PV / baseline demand ratios (comma-separated)','0,0.5,1.0')
        load_levels = st.text_input('Load multipliers (comma-separated)','1.0')
        st.caption('Network, equipment and contract capacities remain fixed. PV capacity stays constant when demand changes. Sweeps retain violations and do not repair cases.')
        submit_study = st.form_submit_button('Run controlled study')
    if submit_study:
        try:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',study_project):
                raise ValueError('Invalid project ID')
            plan=build_study_plan('Effects of PV and demand levels on feeder operation',{
                'count':study_count,'n_loads_min':study_loads,'n_loads_max':study_loads,
                'total_kw_min':study_kw,'total_kw_max':study_kw,'scenario':{'kind':study_kind}},
                [float(x.strip()) for x in pv_levels.split(',')],
                [float(x.strip()) for x in load_levels.split(',')])
            with st.spinner('Freezing base networks and evaluating paired conditions...'):
                study_result=run_study(plan,workspace/'projects'/study_project,study_id)
            st.success(study_result['verified_report'])
        except Exception as exc:
            st.error(redact_text(str(exc)))
    study_files=sorted(workspace.glob('studies/*/summary.json'))+sorted(workspace.glob('projects/*/studies/*/summary.json'))
    if study_files:
        chosen_study=st.selectbox('Select controlled study',study_files,format_func=lambda p:str(p.parent.relative_to(workspace)))
        study_result,study_warning=load_json_record(chosen_study)
        if study_warning:st.warning(redact_text(study_warning))
        if study_result is not None and study_result.get('samples'):
            st.dataframe(study_result.get('combinations',[]),width='stretch')
            csv_path=chosen_study.parent/'comparison.csv'
            if csv_path.exists():
                st.download_button('Download case metrics CSV',csv_path.read_bytes(),file_name=f'{chosen_study.parent.name}.csv')
            zip_path=chosen_study.parent/'dataset.zip'
            if zip_path.exists():
                with zip_path.open('rb') as stream:
                    st.download_button('Download study models ZIP',stream,file_name=f'{chosen_study.parent.name}.zip')
            chosen_case=st.selectbox('Compare conditions',range(len(study_result['samples'])),format_func=lambda i:
                f"{study_result['samples'][i]['base_case_id']} · PV={study_result['samples'][i]['pv_capacity_ratio']} · Load multiplier={study_result['samples'][i]['load_scale']}")
            view=chosen_study.parent/study_result['samples'][chosen_case]['relative_path']/'visualization.html'
            if view.exists():
                components.html(display_saved_network(view,view_context='study'),height=850,scrolling=True)

with reference_tab:
    from feeder_agents.references import list_references,run_reference_case
    st.write('Retains reference voltages, impedances, topology and zero-load junctions. Published benchmarks are not field measurements; schematic positions are not geographic data.')
    evidence = st.selectbox('Source category', ['any','actual_derived','benchmark'])
    reference_rows = list_references(evidence_class=evidence)
    chosen = st.selectbox('Reference feeder', [r['case_id'] for r in reference_rows])
    if chosen:
        selected = next(r for r in reference_rows if r['case_id']==chosen)
        st.json(selected)
        scale = st.number_input('Demand multiplier (P and Q scaled together)', min_value=.05, max_value=5.0, value=1.0)
        reference_id = st.text_input('Reference experiment ID', value=datetime.now().strftime('reference_%Y%m%d_%H%M%S'))
        if st.button('Export and validate reference case', disabled=not selected['statistics']['export_supported']):
            try:
                with st.spinner('Exporting and solving the reference feeder...'):
                    result = run_reference_case(chosen,workspace,reference_id,scale)
                st.write(result['verified_report'])
                directory=Path(result['directory'])
                components.html(display_saved_network(directory/'visualization.html',view_context='reference'),height=850,scrolling=True)
                st.download_button('Download MATPOWER / OpenDSS archive',(directory/'dataset.zip').read_bytes(),file_name=reference_id+'.zip')
            except Exception as exc:
                st.error(redact_text(str(exc)))
