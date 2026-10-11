"""Benchmark output guards protect previous source and result evidence."""
import importlib
from pathlib import Path

import pytest


def test_existing_evidence_is_not_overwritten_before_rerun_rejection(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'scripts'))
    runner=importlib.import_module('run_m94_response_coverage')
    (tmp_path/'suite.json').write_text('{}')
    marker=tmp_path/'source.py';marker.write_text('original source')
    def snapshot(output):
        marker.write_text('replacement source')
        return dict(source_hash='changed',files={})
    monkeypatch.setattr(runner,'source_record',snapshot)
    with pytest.raises(ValueError):
        runner.evaluate([], 'm94', tmp_path, 1)
    assert marker.read_text()=='original source'
