"""Explicit opt-in oracle for the deferred output-memory contract.

This filename intentionally does not match pytest's default test_*.py pattern.
Run: uv run pytest tests/deferred/operational_capture_contract.py -q
Expected current result: FAIL at the unsized-read sentinel. No xfail or skip.
Original red evidence: c0d0fe2 and 25cc61a; see tests/README.md.
"""
from amplifier_bundle_digital_twin_universe import engine, incus
from amplifier_bundle_digital_twin_universe.incus import exec_command as native_exec
from unit.test_operational_capture_contract import fake_spools


def test_successful_provisioning_avoids_unsized_output_reads(monkeypatch):
    # This asserts finite reads, not a proposed public byte limit. Small finite
    # payloads plus the sentinel discriminate without relying on memory pressure.
    monkeypatch.setattr(incus, "exec_command", native_exec)
    with fake_spools(monkeypatch, require_sized_reads=True) as (calls, _):
        engine._run_provisioning("candidate", ["fixture-step"])
        assert calls == [(["incus", "exec", "candidate", "--", "bash", "-lc", "fixture-step"], 600)]
