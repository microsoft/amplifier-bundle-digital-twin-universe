from unittest.mock import MagicMock, patch

import pytest

from amplifier_bundle_digital_twin_universe import incus

ENV = "AMPLIFIER_DTU_INCUS_CONTROL_TIMEOUT_SECONDS"


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, 10),
        ("", 10),
        ("120", 120),
        ("1", 1),
        ("3600", 3600),
        ("-1", 10),
        ("0", 10),
        ("3601", 10),
        ("bad", 10),
    ],
)
def test_control_timeout_policy(monkeypatch, capsys, value, expected):
    if value is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, value)
    assert incus._get_control_timeout_seconds() == expected
    if value in {"-1", "0", "3601", "bad"}:
        assert ENV in capsys.readouterr().err


@pytest.mark.parametrize("operation", ["get", "set"])
def test_launch_metadata_operations_use_configured_deadline(monkeypatch, operation):
    monkeypatch.setenv(ENV, "120")
    result = MagicMock(returncode=0, stdout="value", stderr="")
    with patch.object(incus, "_run", return_value=result) as run:
        if operation == "get":
            assert incus.get_config("owned-test", "user.dtu.id") == "value"
        else:
            incus.set_config("owned-test", "user.dtu.id", "value")
    assert run.call_args.kwargs["timeout"] == 120
