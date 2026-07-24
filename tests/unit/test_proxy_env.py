# Copyright (c) Microsoft. All rights reserved.

"""Unit tests for the proxy-related exports in ``/etc/profile.d/dtu-env.sh``.

``_write_env`` renders the env script that every login shell inside a DTU
sources.  When the proxy is active it exports ``HTTP_PROXY``/``HTTPS_PROXY``
so provisioning traffic is intercepted and ``url_rewrites`` apply.

The loopback exemption is the subject of most of these tests.  Without
``no_proxy``, an in-container client talking to an in-container server on
localhost is routed through mitmproxy, which buffers whole response bodies
and therefore destroys SSE / token streaming for anything running inside
the environment.  Nothing needs loopback traffic proxied: the pypiserver
redirect happens proxy-side (the addon rewrites ``flow.request.host``) and
mock services resolve to the host gateway IP, never loopback.

Run with: uv run pytest tests/unit/test_proxy_env.py -v
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from amplifier_bundle_digital_twin_universe import engine
from amplifier_bundle_digital_twin_universe.profile import (
    Base,
    Passthrough,
    PassthroughService,
    Profile,
)

_LOOPBACK = "localhost,127.0.0.1,::1"


def _make_profile(passthrough: Passthrough | None = None) -> Profile:
    """Minimal profile; only the fields ``_write_env`` reads matter."""
    return Profile(
        path=Path("/tmp/profile.yaml"),
        name="unit-test",
        description="fixture",
        base=Base(image="ubuntu:24.04"),
        passthrough=passthrough,
    )


def _render_env(profile: Profile, *, proxy_enabled: bool) -> str:
    """Run ``_write_env`` and return the script it pushed to the container.

    ``_write_env`` writes a temp file, pushes it, then unlinks it in a
    ``finally``, so the content has to be captured from inside the
    ``file_push`` mock while the file still exists.
    """
    captured: dict[str, str] = {}

    def _capture(_name: str, local_paths: list[str], _dest: str, **_kw: object) -> None:
        captured["script"] = Path(local_paths[0]).read_text()

    with (
        patch.object(engine.incus, "file_push", side_effect=_capture),
        patch.object(engine, "_exec_checked"),
    ):
        engine._write_env("dtu-unit-test", profile, {}, proxy_enabled)

    return captured["script"]


# ---------------------------------------------------------------------------
# Loopback exemption
# ---------------------------------------------------------------------------


def test_no_proxy_exported_when_proxy_enabled() -> None:
    script = _render_env(_make_profile(), proxy_enabled=True)

    assert f'export no_proxy="{_LOOPBACK}"' in script
    assert f'export NO_PROXY="{_LOOPBACK}"' in script


def test_no_proxy_covers_both_ipv4_and_ipv6_loopback() -> None:
    """A client may dial localhost by name, by 127.0.0.1, or by [::1].

    ``no_proxy`` matches on the URL host STRING, not the resolved address,
    so each spelling needs its own entry.
    """
    script = _render_env(_make_profile(), proxy_enabled=True)

    for host in ("localhost", "127.0.0.1", "::1"):
        assert host in script


def test_no_proxy_absent_when_proxy_disabled() -> None:
    """No proxy means nothing to exempt; the script stays minimal."""
    script = _render_env(_make_profile(), proxy_enabled=False)

    assert "no_proxy" not in script
    assert "NO_PROXY" not in script
    assert "HTTP_PROXY" not in script


def test_no_proxy_follows_the_proxy_exports() -> None:
    """Ordering guard: the exemption must not precede what it exempts.

    Both orders happen to work in a sourced script, but keeping the
    exemption adjacent to and after the proxy block is what makes the
    pairing obvious to the next reader.
    """
    script = _render_env(_make_profile(), proxy_enabled=True)

    assert script.index("export HTTP_PROXY=") < script.index("export no_proxy=")


# ---------------------------------------------------------------------------
# Interaction with passthrough overrides
# ---------------------------------------------------------------------------


def test_passthrough_no_proxy_override_wins(monkeypatch) -> None:
    """A profile forwarding the host's own ``no_proxy`` must take precedence.

    Passthrough exports are appended last, and the script is sourced top to
    bottom, so the user's value is the one that survives.
    """
    monkeypatch.setenv("no_proxy", "localhost,example.internal")
    profile = _make_profile(
        passthrough=Passthrough(
            services=[PassthroughService(name="custom", key_env="no_proxy")]
        )
    )

    script = _render_env(profile, proxy_enabled=True)

    ours = script.index(f'export no_proxy="{_LOOPBACK}"')
    theirs = script.index('export no_proxy="localhost,example.internal"')
    assert ours < theirs
