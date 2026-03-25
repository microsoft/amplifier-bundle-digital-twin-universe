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
def launch(profile: str, var: tuple[str, ...], name: str | None) -> None:
    """Launch a new Digital Twin Universe from a profile."""
    variables: dict[str, str] = {}
    for v in var:
        if "=" not in v:
            click.echo(f"Invalid --var format: {v!r}. Expected KEY=VALUE.", err=True)
            sys.exit(1)
        key, _, value = v.partition("=")
        variables[key] = value

    try:
        result = engine.launch(profile, variables, name=name)
        click.echo(json.dumps(result))
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


@main.command(name="exec")
@click.argument("id")
@click.argument("command", nargs=-1)
def exec_(id: str, command: tuple[str, ...]) -> None:
    """Execute a command or start an interactive shell inside a running environment.

    Without a command, attaches a terminal to the container.
    With a command after --, runs it and returns JSON.

    \b
    Examples:
        amplifier-digital-twin exec dtu-a1b2c3d4
        amplifier-digital-twin exec dtu-a1b2c3d4 -- amplifier --version
    """
    if command:
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
def destroy(id: str) -> None:
    """Destroy an environment. Stops and deletes the Incus container and any associated storage."""
    try:
        result = engine.destroy(id)
        click.echo(json.dumps(result))
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
