"""Real DSS coverage for phase connections, sequence voltages and delivered power."""
import pytest

from feeder_agents.schemas import Bus, Feeder, Line, Load, PhasePower
from feeder_agents.simulation import export_dss, simulate


def case(phase_kw=(100, 100, 100), phases=(1, 2, 3), pv=(10, 10, 10)):
    load = Load(id='demand', bus='end', kw=sum(phase_kw), kvar=sum(phase_kw)*.2,
                pv_kw=sum(pv), contract_kva=500).model_copy(update={
        'phases': list(phases), 'phase_powers': [PhasePower(
            phase=p, kw=kw, kvar=kw*.2, pv_kw=g) for p, kw, g in zip(phases, phase_kw, pv)]})
    return Feeder(seed=1, voltage_kv=10, frequency_hz=50, total_kw=sum(phase_kw),
        buses=[Bus(id='source', x_km=0, y_km=0),
               Bus(id='end', x_km=1, y_km=0).model_copy(update={'phases': list(phases)})],
        lines=[Line(id='branch', bus1='source', bus2='end', length_km=1,
                    conductor='research_small').model_copy(update={'phases': list(phases), 'construction': 'illustrative'})],
        loads=[load], assumptions=[]).model_copy(update={'phase_mode': 'unbalanced'})


def test_equal_phase_powers_reproduce_legacy_balanced_solution(tmp_path):
    feeder = case()
    balanced = simulate(export_dss(feeder.model_copy(update={'phase_mode': 'balanced'}), tmp_path/'balanced'))
    actual = simulate(export_dss(feeder, tmp_path/'unbalanced'))
    assert actual['converged']
    for bus, values in balanced['bus_voltage_pu'].items():
        assert actual['bus_voltage_pu'][bus] == pytest.approx(values, abs=2e-8)
    for key in ('source_kw', 'load_kw', 'generation_kw', 'loss_kw'):
        assert actual[key] == pytest.approx(balanced[key], abs=2e-5)
    assert actual['max_vuf_percent'] < 1e-5


def test_asymmetric_load_and_pv_are_delivered_on_requested_phases(tmp_path):
    result = simulate(export_dss(case((240, 45, 15), pv=(0, 12, 3)), tmp_path))
    assert result['converged']
    assert result['load_element_kw'] == pytest.approx({'demand_p1': 240, 'demand_p2': 45, 'demand_p3': 15}, rel=1e-5)
    assert result['generation_element_kw'] == pytest.approx({'pv_demand_p2': 12, 'pv_demand_p3': 3}, rel=1e-5)
    assert result['load_kw'] == pytest.approx(300, rel=1e-5)
    assert result['generation_kw'] == pytest.approx(15, rel=1e-5)
    assert result['source_kw'] == pytest.approx(285+result['loss_kw'], abs=.005)
    assert result['bus_vuf_percent']['end'] > .01
    assert result['vuf_eligible_bus_count'] == 2


@pytest.mark.parametrize('phases,phase_kw,pv', [((3,), (90,), (9,)), ((1, 3), (60, 30), (6, 3))])
def test_partial_phase_branches_preserve_physical_node_labels(tmp_path, phases, phase_kw, pv):
    result = simulate(export_dss(case(phase_kw, phases, pv), tmp_path))
    assert result['converged']
    assert result['bus_phase_nodes']['end'] == list(phases)
    assert set(result['bus_phase_voltage_pu']['end']) == {str(p) for p in phases}
    assert all(.95 < v < 1.01 for v in result['bus_phase_voltage_pu']['end'].values())
    assert set(result['line_phase_current_a']['branch']) == {str(p) for p in phases}
    assert result['load_kw'] == pytest.approx(90, rel=1e-5)
    assert result['generation_kw'] == pytest.approx(9, rel=1e-5)
    assert result['source_kw'] == pytest.approx(81+result['loss_kw'], abs=.005)
    assert result['bus_vuf_percent'].get('end') is None
    assert result['vuf_eligible_bus_count'] == 1
    assert result['vuf_ineligible_bus_count'] == 1
    if phases == (1, 3):
        assert result['line_phase_current_a']['branch']['1'] > 1.9*result['line_phase_current_a']['branch']['3']


def test_unbalanced_partial_phase_tie_is_open_on_every_conductor(tmp_path):
    feeder = case((60, 30), (1, 3), (6, 3))
    feeder.tie_lines = [feeder.lines[0].model_copy(update={'id': 'tie'})]
    result = simulate(export_dss(feeder, tmp_path))
    assert result['line_phase_open_status']['tie'] == {'terminal_1': [True, True], 'terminal_2': [True, True]}
    assert result['line_current_a']['tie'] == pytest.approx(0, abs=1e-6)
    assert result['line_open_status']['branch'] == {'terminal_1': False, 'terminal_2': False}


@pytest.mark.parametrize('mode', ['balanced', 'unbalanced'])
def test_cable_matrix_retains_zero_sequence_and_charging(tmp_path, mode):
    from opendssdirect import dss
    feeder = case().model_copy(update={'phase_mode': mode})
    feeder.lines[0].construction = 'cable'
    master = export_dss(feeder, tmp_path)
    engine = dss.NewContext()
    engine.Basic.AllowChangeDir(False)
    engine.Text.Command(f'Compile "{master}"')
    engine.Lines.Name('branch')
    # Independent hand calculation for the small catalog cable profile:
    # R1=.32,R0=.52; X1=.105,X0=.42; C1=200,C0=100 nF/km.
    assert engine.Lines.RMatrix() == pytest.approx([
        1.16/3, .20/3, .20/3, .20/3, 1.16/3, .20/3, .20/3, .20/3, 1.16/3])
    assert engine.Lines.XMatrix() == pytest.approx([
        .21, .105, .105, .105, .21, .105, .105, .105, .21])
    assert engine.Lines.CMatrix() == pytest.approx([
        500/3, -100/3, -100/3, -100/3, 500/3, -100/3, -100/3, -100/3, 500/3])
    result = simulate(master)
    assert result['converged']
    assert result['source_kw'] == pytest.approx(270+result['loss_kw'], abs=.005)


def test_voltage_unbalance_matches_engine_sequence_voltage(tmp_path):
    from opendssdirect import dss
    master = export_dss(case((240, 45, 15)), tmp_path)
    result = simulate(master)
    engine = dss.NewContext()
    engine.Basic.AllowChangeDir(False)
    engine.Text.Command(f'Compile "{master}"')
    engine.Circuit.SetActiveBus('end')
    zero, positive, negative = engine.Bus.SeqVoltages()
    assert result['bus_vuf_percent']['end'] == pytest.approx(100*negative/positive, abs=1e-10)
