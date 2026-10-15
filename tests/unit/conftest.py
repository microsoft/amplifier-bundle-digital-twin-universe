# Copyright (c) Microsoft. All rights reserved.

import pytest

from amplifier_bundle_digital_twin_universe import incus


@pytest.fixture
def admission_infra(monkeypatch, tmp_path):
    """Isolated admission state and daemon facts; no live Incus dependency."""
    monkeypatch.setenv("AMPLIFIER_DTU_ADMISSION_DIR", str(tmp_path / "admission"))
    for key in (
        "AMPLIFIER_DTU_MAX_RUNNING",
        "AMPLIFIER_DTU_MAX_INSTANCES",
        "AMPLIFIER_DTU_MIN_FREE_BYTES",
    ):
        monkeypatch.delenv(key, raising=False)

    def query(path):
        if path == "/1.0":
            return {
                "environment": {
                    "certificate_fingerprint": "test-daemon",
                    "project": "default",
                }
            }
        if path == "/1.0/profiles/default":
            return {"devices": {"root": {"type": "disk", "path": "/", "pool": "pool"}}}
        return {"space": {"total": 100 * 1024**3, "used": 1 * 1024**3}}

    monkeypatch.setattr(incus, "query", query)
    monkeypatch.setattr(incus, "container_exists", lambda name: False)
