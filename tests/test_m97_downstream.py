"""Independent downstream measurements: unit/sign/allocation and immutable inputs."""
import hashlib
from pathlib import Path

import pytest

from feeder_agents.benchmark.m97_downstream import (
    allocate_q, next_q, voltage_metrics, iteration_behavior, run_case,
    task_variant,
)


@pytest.mark.parametrize('task_id, expected', [
    ('n00_23_urban_witness_GE', 'geometry_witness'),
    ('n00_23_urban_witness_equipment_GE', 'equipment_mixed_witness'),
    ('n01_23_rural_witness_mixed_GE', 'equipment_mixed_witness'),
])
def test_mixed_witness_tasks_are_not_merged_with_geometry(task_id, expected):
    assert task_variant(task_id) == expected


def test_voltage_metrics_count_phases_and_keep_signed_extrema():
    metrics = voltage_metrics([0.94, 1.0, 1.07])
    assert metrics['voltage_mae_pu'] == pytest.approx(0.13 / 3)
    assert metrics['voltage_rmse_pu'] == pytest.approx((0.0085 / 3) ** 0.5)
    assert metrics['voltage_min_pu'] == 0.94
    assert metrics['voltage_max_pu'] == 1.07
    assert metrics['voltage_violation_count'] == 2
    with pytest.raises(ValueError):
        voltage_metrics([float('nan')])


def test_positive_q_feedback_raises_low_voltage_and_enforces_total_limit():
    assert next_q(0.02, 0.99, 5, 0.3) == pytest.approx(0.07)
    assert next_q(0.02, 1.01, 5, 0.3) == pytest.approx(-0.03)
    assert next_q(0.0, 0.9, 100, 0.3) == 0.3
    assert next_q(0.0, 1.1, 100, 0.3) == -0.3
    assert allocate_q(0.3, 3) == pytest.approx([0.1, 0.1, 0.1])
    with pytest.raises(ValueError):
        allocate_q(0.3, 0)


def test_iteration_behavior_distinguishes_fixed_point_from_two_cycle():
    assert not iteration_behavior([0, .1, .15, .175])['oscillatory_tail']
    assert iteration_behavior([.2, -.2] * 8)['oscillatory_tail']
    assert not iteration_behavior([.3] * 12)['oscillatory_tail']


def test_real_dss_q_sign_total_readback_and_inputs_unchanged(tmp_path):
    model = tmp_path / 'model'
    model.mkdir()
    (model / 'Master.dss').write_text('''Clear
Set DefaultBaseFrequency=50
New Circuit.test basekv=10 pu=1 phases=3 bus1=source frequency=50 mvasc3=1000 mvasc1=500
New Line.l1 bus1=source.1.2.3 bus2=b1.1.2.3 phases=3 r1=1 x1=1 r0=2 x0=2 c1=0 c0=0 length=1 units=km normamps=200
New Load.l1_p1 bus1=b1.1.0 phases=1 conn=wye kv=5.7735026919 kw=100 kvar=40 model=1 vminpu=.01 vmaxpu=2
New Load.l1_p2 bus1=b1.2.0 phases=1 conn=wye kv=5.7735026919 kw=100 kvar=40 model=1 vminpu=.01 vmaxpu=2
New Load.l1_p3 bus1=b1.3.0 phases=1 conn=wye kv=5.7735026919 kw=100 kvar=40 model=1 vminpu=.01 vmaxpu=2
Set VoltageBases=[10]
CalcVoltageBases
Set mode=snapshot controlmode=off maxiterations=100 tolerance=1e-8
Solve
''')
    before = hashlib.sha256((model / 'Master.dss').read_bytes()).hexdigest()
    case = {'model_id': 'test', 'network_id': 'test', 'variant': 'ordinary',
            'master_path': str(model / 'Master.dss'), 'voltage_kv': 10,
            'source_bus': 'source', 'ports': [['b1', 1], ['b1', 2], ['b1', 3]],
            'source_model_hashes': {str(model / 'Master.dss'): before},
            'n_buses': 2, 'scene': 'test'}
    scenario = {'id': 'load115', 'load_scale': 1.15, 'phase_weights': None}
    protocol = {'q_limit_mvar': .3, 'max_updates': 80, 'q_tolerance_mvar': 1e-6,
                'voltage_tolerance_pu': 1e-6, 'target_tolerance_pu': 1e-4}
    uncontrolled = run_case(case, scenario, 0, tmp_path / 'off', protocol)
    controlled = run_case(case, scenario, 25, tmp_path / 'on', protocol)
    assert uncontrolled['ac_converged'] and controlled['ac_converged']
    assert controlled['local_voltage_mean_pu'] > uncontrolled['local_voltage_mean_pu']
    assert controlled['actual_q_mvar'] == pytest.approx(controlled['command_q_mvar'], abs=1e-6)
    assert 0 < controlled['actual_q_mvar'] <= .300001
    assert controlled['phase_count'] == 3
    assert controlled['thermal_max_ratio'] < 1
    assert controlled['input_unchanged']
    assert hashlib.sha256((model / 'Master.dss').read_bytes()).hexdigest() == before
    assert (tmp_path / 'on' / 'Final.dss').exists()
