# Copyright (c) Microsoft. All rights reserved.

"""amplifier-digital-twin CLI -- ephemeral Digital Twin Universe management."""

from __future__ import annotations

import json
import sys

import click

from amplifier_bundle_digital_twin_universe import engine


@click.group()
@click.version_option(package_name="amplifier-bundle-digital-twin-universe")
def main() -> None:
    """Manage ephemeral Digital Twin Universe environments from declarative profiles."""


# ---------------------------------------------------------------------------
# Lifecycle commands
# ---------------------------------------------------------------------------


@main.command()
@click.argument("profile")
@click.option(
    "--var",
    multiple=True,
    help="Variable substitution for ${VAR} references in the profile. Format: KEY=VALUE. Repeatable.",
)
@click.option(
    "--name",
    default=None,
    help="Human-readable name. Defaults to dtu-<uuid8>.",
)
@click.option(
    "--hostname",
    default=None,
    help="Hostname for .local mDNS registration. Requires avahi-utils.",
)
def launch(
    profile: str, var: tuple[str, ...], name: str | None, hostname: str | None
) -> None:
    """Launch a new Digital Twin Universe from a profile."""
    variables: dict[str, str] = {}
    for v in var:
        if "=" not in v:
            click.echo(f"Invalid --var format: {v!r}. Expected KEY=VALUE.", err=True)
            sys.exit(1)
        key, _, value = v.partition("=")
        variables[key] = value

    try:
        result = engine.launch(profile, variables, name=name, hostname=hostname)
        click.echo(json.dumps(result))
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


def _parse_stream_timeout(
    ctx: click.Context, param: click.Parameter, value: str
) -> int | None:
    """Parse --timeout for exec --stream.

    Accepts an integer number of seconds, or ``none`` / ``null`` (case-insensitive)
    to disable the timeout entirely.
    """
    if value is None:
        return 600
    normalized = value.strip().lower()
    if normalized in ("none", "null"):
        return None
    try:
        return int(normalized)
    except ValueError as exc:
        raise click.BadParameter(
            f"Invalid timeout: {value!r}. Must be an integer or 'none'."
        ) from exc


@main.command(name="exec")
@click.argument("id")
@click.argument("command", nargs=-1)
@click.option(
    "--stream",
    is_flag=True,
    default=False,
    help="Stream output in real-time instead of returning JSON.",
)
@click.option(
    "--timeout",
    "timeout",
    default="600",
    callback=_parse_stream_timeout,
    help=(
        "Timeout in seconds for --stream mode (default: 600). "
        "Pass 'none' to disable the timeout entirely. "
        "Ignored in interactive and JSON modes."
    ),
)
def exec_(id: str, command: tuple[str, ...], stream: bool, timeout: int | None) -> None:
    """Execute a command or start an interactive shell inside a running environment.

    Without a command, attaches a terminal to the container.
    With a command after --, runs it and returns JSON (default) or streams
    output in real-time (--stream).

    \b
    Examples:
        amplifier-digital-twin exec dtu-a1b2c3d4
        amplifier-digital-twin exec dtu-a1b2c3d4 -- amplifier --version
        amplifier-digital-twin exec --stream dtu-a1b2c3d4 -- amplifier run "prompt"
        amplifier-digital-twin exec --stream --timeout 1800 dtu-a1b2c3d4 -- long-task
        amplifier-digital-twin exec --stream --timeout none dtu-a1b2c3d4 -- long-task
    """
    if command:
        if stream:
            try:
                exit_code = engine.exec_stream(id, list(command), timeout=timeout)
                sys.exit(exit_code)
            except Exception as exc:
                click.echo(f"Error: {exc}", err=True)
                sys.exit(1)
        else:
            try:
                result = engine.exec_command(id, list(command))
                click.echo(json.dumps(result))
            except Exception as exc:
                click.echo(f"Error: {exc}", err=True)
                sys.exit(1)
    else:
        exit_code = engine.exec_interactive(id)
        sys.exit(exit_code)


@main.command()
@click.argument("id")
def status(id: str) -> None:
    """Check whether an environment exists and is running."""
    try:
        result = engine.status(id)
        click.echo(json.dumps(result))
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@main.command(name="list")
def list_() -> None:
    """List all environments managed by this tool."""
    try:
        result = engine.list_environments()
        click.echo(json.dumps(result))
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@main.command(name="check-readiness")
@click.argument("id")
@click.option(
    "--skip-access-check",
    is_flag=True,
    default=False,
    help="Skip host-side access port verification.",
)
def check_readiness(id: str, skip_access_check: bool) -> None:
    """Run readiness checks for an environment.

    Includes host-side verification of access.ports by default.
    Use --skip-access-check to disable.

    Exit codes: 0 = ready, 1 = not ready, 2 = error.
    """
    try:
        result = engine.check_readiness(id, skip_access_check=skip_access_check)
        click.echo(json.dumps(result))
        if result.get("ready") is True:
            sys.exit(0)
        elif result.get("ready") is None:
            sys.exit(0)
        else:
            sys.exit(1)
    except Exception as exc:
        click.echo(json.dumps({"error": str(exc)}), err=True)
        sys.exit(2)


@main.command()
@click.argument("id")
@click.option(
    "--var",
    multiple=True,
    help="Variable substitution for ${VAR} references in the profile. Format: KEY=VALUE. Repeatable.",
)
@click.option(
    "--skip-readiness",
    is_flag=True,
    default=False,
    help="Skip readiness checks after update.",
)
def update(id: str, var: tuple[str, ...], skip_readiness: bool) -> None:
    """Update provisioned software in a running environment.

    Re-runs the profile's update section: optionally refreshes PyPI
    overrides (rebuilds wheels), then executes the update commands.
    Readiness checks are re-run automatically unless --skip-readiness is set.
    """
    variables: dict[str, str] = {}
    for v in var:
        if "=" not in v:
            click.echo(f"Invalid --var format: {v!r}. Expected KEY=VALUE.", err=True)
            sys.exit(1)
        key, _, value = v.partition("=")
        variables[key] = value

    try:
        result = engine.update(id, variables, skip_readiness=skip_readiness)
        click.echo(json.dumps(result))
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------


@main.command(name="file-push")
@click.argument("instance_id")
@click.argument("paths", nargs=-1, required=True)
@click.option(
    "-r/-R",
    "--recursive/--no-recursive",
    default=True,
    help="Recursively transfer files (default: on).",
)
@click.option(
    "-p/-P",
    "--create-dirs/--no-create-dirs",
    default=True,
    help="Create any directories necessary (default: on).",
)
@click.option("--mode", default=None, help="Set file permissions on push.")
@click.option("--uid", type=int, default=None, help="Set file UID on push.")
@click.option("--gid", type=int, default=None, help="Set file GID on push.")
@click.option("--timeout", type=int, default=120, help="Timeout in seconds.")
def file_push(
    instance_id: str,
    paths: tuple[str, ...],
    recursive: bool,
    create_dirs: bool,
    mode: str | None,
    uid: int | None,
    gid: int | None,
    timeout: int,
) -> None:
    """Push files into an instance.

    The last path is the destination inside the container; all preceding
    paths are local sources.

    \b
    Examples:
        amplifier-digital-twin file-push dtu-a1b2 ./config.yaml /root/config.yaml
        amplifier-digital-twin file-push dtu-a1b2 a.txt b.txt /root/data/
        amplifier-digital-twin file-push dtu-a1b2 ./data/ /root/app/data/
    """
    if len(paths) < 2:
        click.echo("Error: need at least one source and a destination.", err=True)
        sys.exit(1)
    local_paths = list(paths[:-1])
    container_path = paths[-1]
    try:
        engine.file_push(
            instance_id,
            local_paths,
            container_path,
            recursive=recursive,
            create_dirs=create_dirs,
            mode=mode,
            uid=uid,
            gid=gid,
            timeout=timeout,
        )
        click.echo(
            json.dumps(
                {
                    "instance_id": instance_id,
                    "sources": local_paths,
                    "dest": container_path,
                    "recursive": recursive,
                }
            )
        )
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@main.command(name="file-pull")
@click.argument("instance_id")
@click.argument("paths", nargs=-1, required=True)
@click.option(
    "-r/-R",
    "--recursive/--no-recursive",
    default=True,
    help="Recursively transfer files (default: on).",
)
@click.option(
    "-p/-P",
    "--create-dirs/--no-create-dirs",
    default=True,
    help="Create any directories necessary (default: on).",
)
@click.option("--timeout", type=int, default=120, help="Timeout in seconds.")
def file_pull(
    instance_id: str,
    paths: tuple[str, ...],
    recursive: bool,
    create_dirs: bool,
    timeout: int,
) -> None:
    """Pull files from an instance.

    The last path is the local destination; all preceding paths are
    container sources.

    \b
    Examples:
        amplifier-digital-twin file-pull dtu-a1b2 /root/output.log ./output.log
        amplifier-digital-twin file-pull dtu-a1b2 -r /root/results/ ./results/
    """
    if len(paths) < 2:
        click.echo("Error: need at least one source and a destination.", err=True)
        sys.exit(1)
    container_paths = list(paths[:-1])
    local_path = paths[-1]
    try:
        engine.file_pull(
            instance_id,
            container_paths,
            local_path,
            recursive=recursive,
            create_dirs=create_dirs,
            timeout=timeout,
        )
        click.echo(
            json.dumps(
                {
                    "instance_id": instance_id,
                    "sources": container_paths,
                    "dest": local_path,
                    "recursive": recursive,
                }
            )
        )
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


@main.command()
@click.argument("id")
def destroy(id: str) -> None:
    """Destroy an environment. Stops and deletes the Incus container and any associated storage."""
    try:
        result = engine.destroy(id)
        click.echo(json.dumps(result))
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
