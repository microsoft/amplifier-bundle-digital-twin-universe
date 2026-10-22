# Copyright (c) Microsoft. All rights reserved.

"""amplifier-digital-twin CLI -- ephemeral Digital Twin Universe management."""

from __future__ import annotations

import json
import sys

import click

from amplifier_bundle_digital_twin_universe import admission, engine


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
    help="Human-readable name. Defaults to dtu-<request hash>.",
)
@click.option(
    "--hostname",
    default=None,
    help="Hostname for .local mDNS registration. Requires avahi-utils.",
)
@click.option(
    "--max-instances",
    "max_instances",
    type=click.IntRange(min=0),
    default=None,
    help=(
        "Total active + retained DTUs ceiling. 0 disables only this ceiling. "
        "Default: AMPLIFIER_DTU_MAX_INSTANCES env var if set, else 256."
    ),
)
@click.option(
    "--max-running",
    type=click.IntRange(min=0),
    default=None,
    help="Active/starting DTUs ceiling (default: AMPLIFIER_DTU_MAX_RUNNING or 15).",
)
@click.option(
    "--min-free-bytes",
    type=click.IntRange(min=0),
    default=None,
    help="Minimum free storage-pool bytes (default: AMPLIFIER_DTU_MIN_FREE_BYTES or 10 GiB).",
)
@click.option(
    "--owner-root",
    default=None,
    help="Root task owner metadata; not an authorization grant.",
)
@click.option(
    "--request-id",
    default=None,
    help="Stable launch request ID. Retrying never repeats provisioning.",
)
@click.option(
    "--offline",
    is_flag=True,
    help="Omit inherited profiles, NICs and host mounts; use only the root storage pool.",
)
def launch(
    profile: str,
    var: tuple[str, ...],
    name: str | None,
    hostname: str | None,
    max_instances: int | None,
    max_running: int | None,
    min_free_bytes: int | None,
    owner_root: str | None,
    request_id: str | None,
    offline: bool,
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
        result = engine.launch(
            profile,
            variables,
            name=name,
            hostname=hostname,
            max_instances=max_instances,
            max_running=max_running,
            min_free_bytes=min_free_bytes,
            owner_root=owner_root,
            request_id=request_id,
            offline=offline,
        )
        click.echo(json.dumps(result))
    except admission.AdmissionError as exc:
        click.echo(json.dumps(exc.as_dict()))
        sys.exit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@main.command()
@click.option("--max-running", type=click.IntRange(min=0), default=None)
@click.option("--max-instances", type=click.IntRange(min=0), default=None)
@click.option("--min-free-bytes", type=click.IntRange(min=0), default=None)
def capacity(max_running, max_instances, min_free_bytes) -> None:
    """Observe execution, retained-instance and storage admission constraints."""
    try:
        click.echo(
            json.dumps(engine.capacity(max_running, max_instances, min_free_bytes))
        )
    except admission.AdmissionError as exc:
        click.echo(json.dumps(exc.as_dict()))
        sys.exit(1)
    except Exception as exc:
        click.echo(
            json.dumps(
                {
                    "schema_version": 1,
                    "error": "backend_unavailable",
                    "message": str(exc),
                }
            )
        )
        sys.exit(1)


@main.command(name="launch-status")
@click.argument("request_id")
def launch_status(request_id: str) -> None:
    """Reconcile a recorded request without launching or provisioning again."""
    try:
        click.echo(json.dumps(engine.launch_status(request_id)))
    except admission.AdmissionError as exc:
        click.echo(json.dumps(exc.as_dict()))
        sys.exit(1)
    except Exception as exc:
        click.echo(
            json.dumps(
                {
                    "schema_version": 1,
                    "error": "backend_unavailable",
                    "message": str(exc),
                }
            )
        )
        sys.exit(1)


def _parse_exec_timeout(
    ctx: click.Context, param: click.Parameter, value: str
) -> int | None:
    """Parse --timeout for exec (JSON and --stream modes).

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
    callback=_parse_exec_timeout,
    help=(
        "Timeout in seconds for --stream and JSON modes (default: 600). "
        "Pass 'none' to disable the timeout entirely. "
        "Ignored in interactive mode."
    ),
)
@click.option(
    "--visual-id",
    "visual_id",
    type=str,
    default=None,
    metavar="LABEL",
    help=(
        "Prepend a blue (dtu:<label>) prefix to the interactive shell prompt "
        "so you can tell DTU sessions apart. Pass an empty string "
        '(--visual-id "") to use the DTU\'s profile name, or a custom string '
        "(--visual-id testing-pr-42) to override it. Interactive mode only "
        "-- ignored in JSON and --stream modes."
    ),
)
def exec_(
    id: str,
    command: tuple[str, ...],
    stream: bool,
    timeout: int | None,
    visual_id: str | None,
) -> None:
    """Execute a command or start an interactive shell inside a running environment.

    Without a command, attaches a terminal to the container.
    With a command after --, runs it and returns JSON (default) or streams
    output in real-time (--stream).

    \b
    Examples:
        amplifier-digital-twin exec dtu-a1b2c3d4
        amplifier-digital-twin exec --visual-id "" dtu-a1b2c3d4
        amplifier-digital-twin exec --visual-id testing-pr-42 dtu-a1b2c3d4
        amplifier-digital-twin exec dtu-a1b2c3d4 -- amplifier --version
        amplifier-digital-twin exec --timeout 1800 dtu-a1b2c3d4 -- long-task
        amplifier-digital-twin exec --timeout none dtu-a1b2c3d4 -- long-task
        amplifier-digital-twin exec --stream dtu-a1b2c3d4 -- amplifier run "prompt"
        amplifier-digital-twin exec --stream --timeout 1800 dtu-a1b2c3d4 -- long-task
        amplifier-digital-twin exec --stream --timeout none dtu-a1b2c3d4 -- long-task
    """
    if command:
        # --visual-id is a prompt-only feature; JSON and stream modes don't use
        # prompts. Accept but ignore.
        if stream:
            try:
                exit_code = engine.exec_stream(id, list(command), timeout=timeout)
                sys.exit(exit_code)
            except Exception as exc:
                click.echo(f"Error: {exc}", err=True)
                sys.exit(1)
        else:
            try:
                result = engine.exec_command(id, list(command), timeout=timeout)
                click.echo(json.dumps(result))
            except Exception as exc:
                click.echo(f"Error: {exc}", err=True)
                sys.exit(1)
    else:
        # visual_id semantics:
        #   None -> flag not passed, no prefix (current default behavior)
        #   ""   -> flag passed with empty value, resolve to profile name
        #   other -> use as-is as the label
        resolved_visual_id: str | None = None
        if visual_id == "":
            try:
                info = engine.status(id)
            except Exception as exc:
                click.echo(f"Error resolving profile for --visual-id: {exc}", err=True)
                sys.exit(1)
            profile_name = info.get("profile") if isinstance(info, dict) else None
            if not profile_name:
                click.echo(
                    "Warning: could not resolve profile name; disabling --visual-id.",
                    err=True,
                )
                resolved_visual_id = None
            else:
                resolved_visual_id = profile_name
        elif visual_id is not None:
            resolved_visual_id = visual_id

        if resolved_visual_id is not None:
            try:
                resolved_visual_id = engine.sanitize_visual_id(resolved_visual_id)
            except ValueError as exc:
                click.echo(f"Error: {exc}", err=True)
                sys.exit(1)

        exit_code = engine.exec_interactive(id, visual_id=resolved_visual_id)
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
    default=False,
    help=(
        "Directories are auto-detected and pushed recursively regardless of "
        "this flag; each lands at <destination>/<basename> (cp -r convention). "
        "With file-only sources, this flag changes the destination semantics: "
        "the destination is treated as a parent directory and each file lands "
        "at <destination>/<filename>. Leave off (default) for exact-path "
        "single-file or multi-file pushes."
    ),
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
    default=False,
    help=(
        "Remote directories are auto-detected and pulled recursively "
        "regardless of this flag; each lands at <local_path>/<basename> "
        "(cp -r convention). For file-only pulls the flag is forwarded to "
        "'incus file pull --recursive'; regular files are unaffected, but "
        "it changes symlink handling: with the flag a symlink source is "
        "recreated as a symlink instead of being dereferenced."
    ),
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
@click.option(
    "--owner-root", default=None, help="Require this exact root owner before deletion."
)
@click.option(
    "--request-id",
    default=None,
    help="Require this exact launch request before deletion.",
)
def destroy(id: str, owner_root: str | None, request_id: str | None) -> None:
    """Destroy an environment. Stops and deletes the Incus container and any associated storage."""
    try:
        result = engine.destroy(id, owner_root=owner_root, request_id=request_id)
        click.echo(json.dumps(result))
    except admission.AdmissionError as exc:
        click.echo(json.dumps(exc.as_dict()))
        sys.exit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
