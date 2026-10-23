# Copyright (c) Microsoft. All rights reserved.

"""Binding-only tests: temporary files and mocked run; no programs or sockets."""

import copy
import hashlib
import json
import os
import subprocess
from contextvars import Context
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from amplifier_bundle_digital_twin_universe import bound_backend as backend
from amplifier_bundle_digital_twin_universe import incus


@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
    # A missed mock must fail rather than spawn an executable.
    monkeypatch.setattr(subprocess, "Popen", Mock(side_effect=AssertionError("No process may spawn")))
    run = Mock(side_effect=AssertionError("Unexpected subprocess.run"))
    monkeypatch.setattr(subprocess, "run", run)
    return run


@pytest.fixture
def client(tmp_path, monkeypatch, no_processes):
    for key in list(os.environ):
        if key.startswith("INCUS") or key in {
            "AMPLIFIER_DTU_INCUS_PROJECT", backend._TIMEOUT_ENV,
        }:
            monkeypatch.delenv(key)
    config = tmp_path / "client"
    global_config = tmp_path / "global"
    config.mkdir(mode=0o700)
    global_config.mkdir(mode=0o700)
    binary = tmp_path / "incus"
    binary.write_bytes(b"not executable code; subprocess.run is mocked\n")
    binary.chmod(0o700)
    settings = {
        "default-remote": "local",
        "remotes": {"local": {
            "addr": "unix:///private/incus/unix.socket",
            "protocol": "incus", "public": False, "project": "owned",
        }},
        "aliases": {},
    }

    def write(value=None, *, directory=config):
        path = directory / "config.yml"
        path.write_text(yaml.safe_dump(settings if value is None else value))
        path.chmod(0o600)
        return path

    write()
    monkeypatch.setenv("INCUS_CONF", str(config))
    monkeypatch.setenv("INCUS_GLOBAL_CONF", str(global_config))
    monkeypatch.setattr(backend.shutil, "which", lambda name: str(binary))
    environment = {"certificate_fingerprint": "a" * 64, "project": "owned"}
    devices = {"root": {"type": "disk", "path": "/", "pool": "pool"}}

    def reply(command, **kwargs):
        if command[1] == "query" and command[2].split("?")[0] == "/1.0":
            value = {"environment": environment}
        elif command[1] == "query" and command[2].split("?")[0] == "/1.0/profiles/default":
            value = {"devices": devices}
        else:
            return subprocess.CompletedProcess(command, 0, "command-result", "")
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

    no_processes.side_effect = reply
    return SimpleNamespace(
        config=config, global_config=global_config, binary=binary,
        settings=settings, write=write, environment=environment,
        devices=devices, run=no_processes, reply=reply,
    )


def test_capture_exact_binding_and_private_roundtrip(client):
    bound = backend.capture()
    assert isinstance(bound, backend.BoundBackend)
    assert issubclass(backend.BackendFence, incus.IncusError)
    assert bound.scope == {"daemon": "a" * 64, "project": "owned", "storage_pool": "pool"}
    record = bound.as_dict()
    assert record["executable"]["path"] == str(client.binary.resolve())
    assert record["executable"]["identity"]["sha256"] == hashlib.sha256(client.binary.read_bytes()).hexdigest()
    assert record["global_config"]["file"] is None
    assert record["socket"] == "/private/incus/unix.socket"
    assert len(bound.digest) == 64
    client.run.reset_mock()
    restored = backend.from_dict(json.loads(json.dumps(record)))
    assert restored.as_dict() == record
    assert backend.BoundBackend.from_dict(record).digest == bound.digest
    client.run.assert_not_called()
    record["scope"]["project"] = "tampered"
    assert bound.scope["project"] == restored.scope["project"] == "owned"
    assert str(client.config) not in repr(bound)


@pytest.mark.parametrize("query_failure", [False, True])
def test_execution_timeout_category_does_not_classify_identity_query_timeout(client, query_failure):
    bound = backend.capture()
    def run(command, **kwargs):
        if query_failure or command[1] != "query":
            raise subprocess.TimeoutExpired("SECRET-ARGV", 1, output="SECRET-OUTPUT")
        return client.reply(command, **kwargs)
    client.run.side_effect = run
    with pytest.raises(backend.BackendFence) as caught:
        bound.run(["incus", "launch", "local:" + "a" * 64, "owned"], timeout=1)
    assert isinstance(caught.value, backend.BackendExecutionTimeout) is (not query_failure)
    assert "SECRET" not in str(caught.value)


def test_queries_use_pinned_executable_direct_metadata_and_explicit_project(client, monkeypatch):
    monkeypatch.setattr(incus, "_run", Mock(side_effect=AssertionError("Recursion")))
    monkeypatch.setattr(incus, "query", Mock(side_effect=AssertionError("Recursion")))
    bound = backend.capture()
    client.run.reset_mock()
    with bound.activate():
        bound.verify()
    calls = client.run.call_args_list
    assert [c.args[0] for c in calls] == [
        [str(client.binary), "query", "/1.0?project=owned"],
        [str(client.binary), "query", "/1.0/profiles/default?project=owned"],
    ]
    assert all(c.kwargs["stdin"] == subprocess.DEVNULL for c in calls)
    assert all(c.kwargs["check"] is False for c in calls)
    assert all(c.kwargs["env"]["INCUS_REMOTE"] == "local" for c in calls)


def test_ambient_changes_are_ignored_and_never_serialized(client, monkeypatch):
    bound = backend.capture()
    for key in (
        "PATH", "HOME", "INCUS_CONF", "INCUS_GLOBAL_CONF", "INCUS_REMOTE",
        "INCUS_PROJECT", "INCUS_SOCKET", "INCUS_DIR", "INCUS_EXEC_PATH",
        "INCUS_ALIASES", "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
        "ALL_PROXY", "LD_PRELOAD", "PYTHONPATH", "AMPLIFIER_DTU_INCUS_PROJECT",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.setenv(key, "hostile-secret")
    monkeypatch.setenv(backend._TIMEOUT_ENV, "1")
    monkeypatch.setattr(backend.shutil, "which", Mock(side_effect=AssertionError("Do not re-resolve")))
    client.run.reset_mock()
    bound.run(["incus", "stop", "owned-instance"], timeout=9)
    assert len(client.run.call_args_list) == 3
    for call in client.run.call_args_list:
        assert call.args[0][0] == str(client.binary)
        assert call.kwargs["env"] == {
            "PATH": "/usr/bin:/bin", "LC_ALL": "C", "HOME": str(client.config),
            "INCUS_CONF": str(client.config), "INCUS_GLOBAL_CONF": str(client.global_config),
            "INCUS_REMOTE": "local", "INCUS_PROJECT": "owned",
        }
    assert client.run.call_args_list[0].kwargs["timeout"] == 30
    assert "hostile-secret" not in json.dumps(bound.as_dict())


def test_context_is_nested_restored_and_not_installed_globally(client):
    first = backend.capture()
    second = backend.from_dict(first.as_dict())
    assert backend.current() is None
    before = dict(os.environ)
    with first.activate() as active:
        assert active is first and backend.current() is first
        assert Context().run(backend.current) is None
        with pytest.raises(RuntimeError):
            with second.activate():
                assert backend.current() is second
                raise RuntimeError("leave")
        assert backend.current() is first
    assert backend.current() is None
    assert dict(os.environ) == before


@pytest.mark.parametrize("variable", ["INCUS_CONF", "INCUS_GLOBAL_CONF"])
@pytest.mark.parametrize("value", [None, "", "relative"])
def test_both_directories_must_be_explicit(client, monkeypatch, variable, value):
    if value is None:
        monkeypatch.delenv(variable)
    else:
        monkeypatch.setenv(variable, value)
    with pytest.raises(backend.BackendFence):
        backend.capture()
    client.run.assert_not_called()


@pytest.mark.parametrize("target", ["config-directory", "global-directory", "config-file"])
def test_bootstrap_must_be_private(client, target):
    path = {
        "config-directory": client.config, "global-directory": client.global_config,
        "config-file": client.config / "config.yml",
    }[target]
    path.chmod(0o755 if path.is_dir() else 0o644)
    with pytest.raises(backend.BackendFence):
        backend.capture()
    client.run.assert_not_called()


def test_global_config_absence_or_equivalence_only(client):
    client.write(directory=client.global_config)
    bound = backend.capture()
    assert bound.as_dict()["global_config"]["file"] is not None
    client.run.reset_mock()
    conflicting = copy.deepcopy(client.settings)
    conflicting["remotes"]["local"]["project"] = "other"
    client.write(conflicting, directory=client.global_config)
    with pytest.raises(backend.BackendFence):
        backend.capture()
    client.run.assert_not_called()


@pytest.mark.parametrize("change", [
    "client-content", "client-replaced", "client-missing", "client-mode",
    "global-appeared", "directory-replaced", "executable-content",
    "executable-replaced", "executable-mode", "executable-missing",
])
def test_local_drift_fences_before_any_subprocess(client, change):
    bound = backend.capture()
    path = client.config / "config.yml"
    if change == "client-content":
        path.write_text(path.read_text() + "\n# changed\n")
    elif change == "client-replaced":
        path.rename(client.config / "old.yml")
        client.write()
    elif change == "client-missing":
        path.unlink()
    elif change == "client-mode":
        path.chmod(0o644)
    elif change == "global-appeared":
        client.write(directory=client.global_config)
    elif change == "directory-replaced":
        client.config.rename(client.config.with_name("old-client"))
        client.config.mkdir(mode=0o700)
        client.write()
    elif change == "executable-content":
        client.binary.write_bytes(b"changed")
    elif change == "executable-replaced":
        data = client.binary.read_bytes()
        client.binary.rename(client.binary.with_name("old-incus"))
        client.binary.write_bytes(data)
        client.binary.chmod(0o700)
    elif change == "executable-mode":
        client.binary.chmod(0o777)
    elif change == "executable-missing":
        client.binary.unlink()
    client.run.reset_mock()
    with pytest.raises(backend.BackendFence) as raised:
        bound.run(["incus", "delete", "owned-instance"])
    assert str(client.config) not in str(raised.value)
    assert str(client.binary) not in str(raised.value)
    client.run.assert_not_called()


def test_global_config_disappearance_is_not_equivalent_to_pinned_presence(client):
    client.write(directory=client.global_config)
    bound = backend.capture()
    (client.global_config / "config.yml").unlink()
    client.run.reset_mock()
    with pytest.raises(backend.BackendFence):
        bound.verify()
    client.run.assert_not_called()


@pytest.mark.parametrize("what", ["daemon", "project", "pool"])
def test_backend_identity_drift_blocks_requested_command(client, what):
    bound = backend.capture()
    if what == "daemon":
        client.environment["certificate_fingerprint"] = "b" * 64
    elif what == "project":
        client.environment["project"] = "other"
    else:
        client.devices["root"]["pool"] = "other"
    client.run.reset_mock()
    with pytest.raises(backend.BackendFence):
        bound.run(["incus", "delete", "owned-instance"])
    assert all(c.args[0][1] == "query" for c in client.run.call_args_list)


@pytest.mark.parametrize("stdout", [
    "", "not-json", "[]", "null", "{}", '{"environment":[]}',
    '{"environment":{"certificate_fingerprint":"not-a-fingerprint","project":"owned"}}',
    '{"environment":{"certificate_fingerprint":"' + "a" * 64 + '"}}',
    '{"type":"error","status_code":200,"metadata":{}}',
    '{"environment":{},"environment":{}}',
    '{"environment":{"certificate_fingerprint":"' + "a" * 64 + '","project":"owned"},"error":"failed"}',
    '{"environment":{"certificate_fingerprint":"' + "a" * 64 + '","project":"owned"},"other":NaN}',
])
def test_exit_zero_does_not_make_malformed_identity_valid(client, stdout):
    client.run.side_effect = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout, "")
    with pytest.raises(backend.BackendFence):
        backend.capture()


@pytest.mark.parametrize("symbol", ["\u00e9", "\U00010000"])
def test_identity_query_limit_counts_raw_utf8_bytes_not_characters(client, symbol):
    value = {"environment": client.environment, "padding": symbol * (backend._QUERY_LIMIT // 2)}
    stdout = json.dumps(value, ensure_ascii=False)
    assert len(stdout) < backend._QUERY_LIMIT < len(stdout.encode("utf-8"))
    def reply(command, **kwargs):
        if command[2].split("?")[0] == "/1.0":
            return subprocess.CompletedProcess(command, 0, stdout, "")
        return client.reply(command, **kwargs)
    client.run.side_effect = reply
    with pytest.raises(backend.BackendFence, match="exceeds its bound"):
        backend.capture()


@pytest.mark.parametrize("devices", [
    None, [], {}, {"root": "disk"}, {"root": {"type": "disk", "path": "/"}},
    {"root": {"type": "disk", "path": "/", "pool": ""}},
    {"a": {"type": "disk", "path": "/", "pool": "pool"},
     "b": {"type": "disk", "path": "/", "pool": "pool"}},
])
def test_root_pool_observation_must_be_unambiguous(client, devices):
    def reply(command, **kwargs):
        if "/profiles/default" in command[2]:
            return subprocess.CompletedProcess(command, 0, json.dumps({"devices": devices}), "")
        return client.reply(command, **kwargs)
    client.run.side_effect = reply
    with pytest.raises(backend.BackendFence):
        backend.capture()


def test_valid_rest_envelope_is_also_supported(client):
    def reply(command, **kwargs):
        result = client.reply(command, **kwargs)
        value = {"type": "sync", "status_code": 200, "metadata": json.loads(result.stdout)}
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")
    client.run.side_effect = reply
    assert backend.capture().scope["project"] == "owned"


@pytest.mark.parametrize("address", [
    "unix://", "unix://relative", "https://host:8443",
    "unix:///one,unix:///two", ["unix:///one"], "unix:////one",
    "unix:///one?other=two", "unix:///one/../two",
])
def test_implicit_remote_or_multiple_addresses_are_rejected(client, address):
    client.settings["remotes"]["local"]["addr"] = address
    client.write()
    with pytest.raises(backend.BackendFence):
        backend.capture()
    client.run.assert_not_called()


@pytest.mark.parametrize("change", ["default", "extra", "alias", "helper", "defaults", "addrs"])
def test_remote_aliases_helpers_and_defaults_are_rejected(client, change):
    if change == "default":
        client.settings["default-remote"] = "other"
    elif change == "extra":
        client.settings["remotes"]["other"] = {"addr": "unix:///other"}
    elif change == "alias":
        client.settings["aliases"] = {"query": "!untrusted-command"}
    elif change == "helper":
        client.settings["remotes"]["local"]["credentials_helper"] = "untrusted-command"
    elif change == "defaults":
        client.settings["defaults"] = {"console_spice_command": "untrusted-command"}
    else:
        client.settings["remotes"]["local"]["addrs"] = ["unix:///other"]
    client.write()
    with pytest.raises(backend.BackendFence):
        backend.capture()
    client.run.assert_not_called()


@pytest.mark.parametrize("text", [
    "default-remote: local\ndefault-remote: other\n",
    "aliases: &a {}\nremotes: *a\n",
    "!!python/object/apply:os.system ['not-run']",
    "x: " + "[" * 30 + "]" * 30,
    "x: " + "a" * (64 * 1024),
])
def test_unsafe_or_unbounded_yaml_never_invokes_cli(client, text):
    (client.config / "config.yml").write_text(text)
    with pytest.raises(backend.BackendFence):
        backend.capture()
    client.run.assert_not_called()


@pytest.mark.parametrize("kwargs", [
    {"env": {}}, {"env": None}, {"shell": True}, {"shell": False},
    {"executable": "/other"}, {"cwd": "/other"},
    {"preexec_fn": lambda: None}, {"pass_fds": (3,)},
])
def test_execution_overrides_are_rejected_before_queries(client, kwargs):
    bound = backend.capture()
    client.run.reset_mock()
    with pytest.raises(backend.BackendFence):
        bound.run(["incus", "stop", "one"], **kwargs)
    client.run.assert_not_called()


@pytest.mark.parametrize("command", [
    "incus stop one", [], ["other", "stop", "one"], ["incus"],
    ["incus", "--force-local", "stop", "one"],
    ["incus", "stop", "other:one"], ["incus", "stop", "--project=other", "one"],
    ["incus", "stop", "--project", "owned", "one"],
    ["incus", "stop", "--force-local", "one"],
    ["incus", "query", "other:/1.0"], ["incus", "query", "https://host/1.0"],
    ["incus", "query", "/1.0?project=other"],
    ["incus", "query", "/1.0?%70roject=other"],
    ["incus", "query", "/1.0?project=owned&project=owned"],
    ["incus", "query", "/1.0#"],
    ["incus", "remote", "switch", "local"], ["incus", "project", "switch", "owned"],
    ["incus", "alias", "add", "query", "!shell"],
])
def test_routing_overrides_and_client_mutations_are_rejected(client, command):
    bound = backend.capture()
    client.run.reset_mock()
    with pytest.raises(backend.BackendFence):
        bound.run(command)
    client.run.assert_not_called()


def test_positional_popen_options_are_refused(client):
    bound = backend.capture()
    client.run.reset_mock()
    with pytest.raises(backend.BackendFence):
        bound.run(["incus", "stop", "one"], -1)
    client.run.assert_not_called()


def test_run_preserves_stdin_stream_options_and_guest_arguments(client):
    bound = backend.capture()
    command = ["incus", "exec", "one", "--", "sh", "-c", "printf '%s' --project=guest"]
    client.run.reset_mock()
    result = bound.run(command, input="payload", text=True, capture_output=True, timeout=91)
    assert result.stdout == "command-result"
    call = client.run.call_args
    assert call.args[0] == [str(client.binary), "--project", "owned", *command[1:]]
    assert call.kwargs["input"] == "payload"
    assert call.kwargs["capture_output"] is True
    assert call.kwargs["timeout"] == 91
    assert "stdin" not in call.kwargs
    assert command[0] == "incus"
    bound.run(["incus", "file", "push", "-", "one/file"], stdin=subprocess.DEVNULL, stdout=17)
    assert client.run.call_args.kwargs["stdin"] == subprocess.DEVNULL
    assert client.run.call_args.kwargs["stdout"] == 17


@pytest.mark.parametrize("path,expected", [
    ("/1.0", "/1.0?project=owned"),
    ("/1.0?", "/1.0?project=owned"),
    ("/1.0/instances?recursion=1", "/1.0/instances?recursion=1&project=owned"),
    ("/1.0?project=owned", "/1.0?project=owned"),
])
def test_run_query_injects_project_without_cli_flag_or_duplicate(client, path, expected):
    bound = backend.capture()
    bound.run(["incus", "query", path], capture_output=True)
    assert client.run.call_args.args[0] == [str(client.binary), "query", expected]


def test_every_command_reverifies_and_drift_during_query_prevents_effect(client):
    bound = backend.capture()
    client.run.reset_mock()
    bound.run(["incus", "info", "one"])
    bound.run(["incus", "info", "two"])
    assert [c.args[0][1] for c in client.run.call_args_list] == [
        "query", "query", "--project", "query", "query", "--project",
    ]
    def drift(command, **kwargs):
        result = client.reply(command, **kwargs)
        (client.config / "config.yml").write_text("changed")
        return result
    client.run.reset_mock()
    client.run.side_effect = drift
    with pytest.raises(backend.BackendFence):
        bound.run(["incus", "delete", "one"])
    assert client.run.call_count == 1


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "oserror"])
def test_query_errors_are_sanitized_with_no_retry(client, failure):
    raw = "private-path credential-value"
    if failure == "nonzero":
        client.run.side_effect = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", raw)
    elif failure == "timeout":
        client.run.side_effect = subprocess.TimeoutExpired(raw, 30, output=raw)
    else:
        client.run.side_effect = OSError(raw)
    with pytest.raises(backend.BackendFence) as raised:
        backend.capture()
    assert raw not in str(raised.value)
    assert raised.value.__cause__ is None
    assert client.run.call_count == 1


def test_record_is_shape_checked_without_io(client, monkeypatch):
    record = backend.capture().as_dict()
    client.run.reset_mock()
    monkeypatch.setattr(backend, "_local_evidence", Mock(side_effect=AssertionError("No IO")))
    assert backend.from_dict(record).scope["project"] == "owned"
    for invalid in (
        None, {}, {**record, "version": True}, {**record, "version": 2},
        {**record, "env": {"SECRET": "do-not-save"}},
        {**record, "remote": "other"}, {**record, "query_timeout": False},
        {**record, "socket": "unix://relative"},
        {**record, "scope": {**record["scope"], "project": ""}},
        {**record, "executable": {"path": "/other", "identity": {}}},
    ):
        with pytest.raises(backend.BackendFence):
            backend.from_dict(invalid)
    client.run.assert_not_called()


def test_explicit_project_precedence_and_frozen_query_timeout(client, monkeypatch):
    monkeypatch.setenv("INCUS_PROJECT", "environment")
    monkeypatch.setenv("AMPLIFIER_DTU_INCUS_PROJECT", "explicit")
    monkeypatch.setenv(backend._TIMEOUT_ENV, "120")
    client.environment["project"] = "explicit"
    bound = backend.capture()
    assert bound.scope["project"] == "explicit"
    assert bound.as_dict()["query_timeout"] == 120
    assert all(c.kwargs["timeout"] == 120 for c in client.run.call_args_list)


def test_nonlocal_environment_selection_rejected(client, monkeypatch):
    monkeypatch.setenv("INCUS_REMOTE", "other")
    with pytest.raises(backend.BackendFence):
        backend.capture()
    client.run.assert_not_called()


@pytest.mark.parametrize("kind", ["file-symlink", "directory-symlink", "hardlink"])
def test_config_aliases_are_rejected_before_queries(client, monkeypatch, kind):
    path = client.config / "config.yml"
    if kind == "file-symlink":
        original = client.config / "original.yml"
        path.rename(original)
        path.symlink_to(original)
    elif kind == "directory-symlink":
        alias = client.config.with_name("alias")
        alias.symlink_to(client.config, target_is_directory=True)
        monkeypatch.setenv("INCUS_CONF", str(alias))
    else:
        os.link(path, client.config / "linked.yml")
    with pytest.raises(backend.BackendFence):
        backend.capture()
    client.run.assert_not_called()


def test_resolved_executable_is_used_even_when_search_alias_moves(client, monkeypatch):
    alias = client.binary.with_name("incus-link")
    alias.symlink_to(client.binary)
    monkeypatch.setattr(backend.shutil, "which", lambda name: str(alias))
    bound = backend.capture()
    alias.unlink()
    alias.symlink_to("/does-not-exist")
    bound.verify()
    assert all(c.args[0][0] == str(client.binary) for c in client.run.call_args_list)


def test_bound_record_restore_does_not_silently_recapture_drift(client):
    saved = backend.capture().as_dict()
    client.binary.write_bytes(b"changed executable")
    restored = backend.from_dict(saved)
    client.run.reset_mock()
    with pytest.raises(backend.BackendFence):
        restored.run(["incus", "delete", "one"])
    client.run.assert_not_called()


def test_capture_does_not_write_configuration_or_credentials(client):
    before = {
        directory: {path.name: path.read_bytes() for path in directory.iterdir()}
        for directory in (client.config, client.global_config)
    }
    bound = backend.capture()
    bound.run(["incus", "info", "one"])
    after = {
        directory: {path.name: path.read_bytes() for path in directory.iterdir()}
        for directory in (client.config, client.global_config)
    }
    assert after == before


def test_local_target_and_metadata_colons_are_preserved(client):
    bound = backend.capture()
    command = ["incus", "config", "set", "local:one", "user.created=2026-10-22T10:00:00Z"]
    bound.run(command)
    assert client.run.call_args.args[0][3:] == command[1:]


def test_run_returns_nonzero_status_but_never_retries_uncertain_execution(client):
    bound = backend.capture()

    def nonzero(command, **kwargs):
        if command[1] == "query":
            return client.reply(command, **kwargs)
        return subprocess.CompletedProcess(command, 19, "", "command failure")

    client.run.side_effect = nonzero
    assert bound.run(["incus", "info", "missing"]).returncode == 19

    def timeout(command, **kwargs):
        if command[1] == "query":
            return client.reply(command, **kwargs)
        raise subprocess.TimeoutExpired("private argument", 1, output="secret")

    client.run.side_effect = timeout
    client.run.reset_mock()
    with pytest.raises(backend.BackendFence, match="outcome may be unknown") as raised:
        bound.run(["incus", "stop", "one"])
    assert "private argument" not in str(raised.value)
    assert "secret" not in str(raised.value)
    assert client.run.call_count == 3


@pytest.mark.parametrize("timeout", ["", "invalid", "0", "-1", "3601"])
def test_invalid_observation_timeout_is_rejected_before_queries(client, monkeypatch, timeout):
    monkeypatch.setenv(backend._TIMEOUT_ENV, timeout)
    with pytest.raises(backend.BackendFence):
        backend.capture()
    client.run.assert_not_called()