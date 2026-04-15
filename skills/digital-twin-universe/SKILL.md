---
name: digital-twin-universe
description: Use when the user needs an isolated, realistic deployment environment to test code beyond "tests pass on my machine." Covers launching environments from declarative profiles, executing commands inside them, managing their lifecycle, debugging networking or provisioning issues, and working with Incus containers. Also use when the user wants to simulate an end-user experience (e.g. Amplifier CLI, web UIs) without touching production infrastructure. Triggers on digital twin, DTU, isolated environment, simulation environment, amplifier-digital-twin, incus container, profile launch, test in realistic environment, deploy simulation. ALWAYS use this skill whenever a "Digital Twin" is mentioned!
user-invocable: true
---

# Digital Twin Universe Environments

`amplifier-digital-twin` is a CLI for on-demand, isolated environments launched from declarative profiles. Environments can be updated in-place (pull fresh code, reinstall) without destroying and relaunching. All commands output JSON to stdout.

## Prerequisites Check

Before any DTU operation, verify the environment:

```bash
# 1. Double check the CLI is installed
which amplifier-digital-twin

# 2. Is Incus available and running?
which incus && incus version && echo "Incus OK" || echo "Incus NOT available"
```

If `amplifier-digital-twin` is not found:
```bash
uv tool install git+https://github.com/microsoft/amplifier-bundle-digital-twin-universe@main
```

### Installing Incus

If the user doesn't have Incus installed, walk them through the platform-specific steps in the install guide. Present sudo commands to the user one at a time.
If their system is not in the documentation, go to the actual documentation at https://linuxcontainers.org/incus/docs/main/installing/

```
read_file("@digital-twin-universe:docs/installing-incus.md")
```

After the user completes the install steps, run the verification commands yourself (`incus version`, launch a test container, exec into it, delete it). Report results to the user rather than asking them to run verification.

### Installing Docker

Docker is only needed for profiles that use Gitea repos or mock service sidecars. Check if it's already installed with `docker version`. 
You should encourage the user to install Docker since some circumstances will require it.
If it's not installed, walk the user through the install guide:

```
read_file("@digital-twin-universe:docs/installing-docker.md")
```

After installing, make sure to test to see if its working as expected.

**If prerequisites are missing, report clearly and stop. Do not attempt workarounds.**

## When to Use Gitea with a Digital Twin

The Gitea bundle is included as a dependency of this bundle. Use Gitea whenever
the project has **local repos that need to be tested as if they were already
published on GitHub**. Gitea serves it locally so the Digital Twin can consume it without pushing to origin.

Common scenarios:
- Verifying that unpublished code installs and runs correctly in isolation as if they were published to GitHub or PyPI
- Amplifier specific: Testing local changes to a bundle, module, or app before pushing

**NEVER push changes to upstream just to make them available inside a Digital Twin.**
Clone the local repos into Gitea first, then reference those in the profile via
`url_rewrites` or `pypi_overrides`. Load the `gitea` skill for full CLI usage.

If the use case warrants it, you should FIRST load the `gitea` skill and setup the environment for the user.

## Documentation

For overview, quick start, installation, and feature list:

```
read_file("@digital-twin-universe:README.md")
```

For complete CLI reference with all flags and output schemas:

```
read_file("@digital-twin-universe:docs/api-reference.md")
```

For the full profile schema and field reference:

```
read_file("@digital-twin-universe:docs/profiles.md")
```

For building mock services (Docker sidecars with domain interception) and discovering community-published mocks:

```
read_file("@digital-twin-universe:docs/mock-authoring.md")
```

For running Docker inside a Digital Twin Universe environment (nested containers), including platform-specific setup and troubleshooting:

```
read_file("@digital-twin-universe:docs/docker-in-incus.md")
```

## Hostname Support (mDNS)

Environments can register a `.local` hostname via Avahi mDNS, making it easy to
tell multiple DTU instances apart (e.g. `http://my-app.local:8410/` instead of
`http://localhost:8410/`).

**Prerequisites:** `avahi-daemon` and `avahi-utils` must be installed:
```bash
which avahi-publish-address && echo "Avahi OK" || echo "Install: sudo apt install avahi-daemon avahi-utils"
```

**Usage:** Set `access.hostname` in the profile or pass `--hostname` on the CLI:
```bash
amplifier-digital-twin launch my-profile --hostname my-app
# => access URLs will be http://my-app.local:<port>/...
```

If Avahi is not installed, hostname registration is silently skipped and access
URLs fall back to `localhost`. No error, no failure -- it's a graceful degradation.

**Platform support:**
- Native Linux: fully supported (LAN-wide resolution via mDNS)
- WSL2: works within WSL2; Windows browsers cannot resolve `.local` names from WSL2
- macOS/Windows: not supported (warning printed, falls back to localhost)

## Updating Running Environments

Profiles can define an `update` section with commands to pull fresh code and
reinstall without destroying the environment:

```bash
amplifier-digital-twin update <id> [--var K=V ...] [--skip-readiness]
```

This enables a fast `launch` -> `update` -> `test` iteration loop. If the
profile has `refresh_pypi: true` in the `update` section, PyPI overrides are
rebuilt from the current state of the source repos before running update commands.

See `api-reference.md` for the full `update` command reference and `profiles.md`
for the `update` profile schema.


## Example Profiles

When constructing profiles, read the most relevant examples first to understand established patterns:

```
read_file("@digital-twin-universe:profiles/amplifier-user-sim.yaml")
read_file("@digital-twin-universe:profiles/amplifier-chat.yaml")
read_file("@digital-twin-universe:profiles/private-github-repo.yaml")
read_file("@digital-twin-universe:profiles/docker-in-incus.yaml")
```

The `private-github-repo` profile shows how to install from a private GitHub
repo without Gitea. It passes `GH_TOKEN` via `passthrough.services` and
configures `git config --global url...insteadOf` to authenticate all clones.
Use this pattern when you need to test the pushed state of a private repo.
For testing local uncommitted changes, use Gitea + `url_rewrites` instead.

The `docker-in-incus` profile is a minimal test for running Docker containers
inside an Incus-based environment. Use it to verify that nested container
networking works on a given host before attempting more complex profiles that
depend on Docker.

## Profile Placement Convention

When generating or saving DTU profiles, use this default path:

```
.amplifier/digital-twin-universe/profiles/<profile-name>.yaml
```

This path is relative to the workspace or current working directory. Create the
directory structure if it doesn't exist.

Do not commit generated profiles by default. Profiles are often workspace-specific and ephemeral. 
If the user explicitly wants a profile shipped with a repo, default to placing it
at `<repo>/.amplifier/digital-twin-universe/profiles/<profile-name>.yaml`.

## Agents

For specialized DTU tasks within Amplifier sessions, you **MUST** use these agents instead of driving the CLI manually:

- **`dtu-profile-builder`** — Explores a user's project repo, generates a DTU profile, launches the environment, and hands back access details. Use when the user has a project and wants to create a digital twin for it.


## Cleanup Safety

`amplifier-digital-twin list` returns **all** DTU environments on the machine,
not just ones from your session. Other users or concurrent sessions may have
running instances.

**Safe pattern:** Note the `id` returned by `launch` and only `destroy` that
specific ID when you are done.

**Dangerous pattern:** Iterating `list` and destroying every entry. This will
tear down environments belonging to other sessions.

There is currently no owner or session identifier in the `list` output. If you
need to identify your instance, match on the `id` you received from `launch`,
or use `created_at` to narrow down which instance is yours. 
If you are unsure, leave it and tell the user to clean up when they are ready.


## Troubleshooting

### Docker Inside a Digital Twin Universe Environment

If a profile needs to run Docker inside the Incus container (e.g. spawning
worker containers, running Docker Compose stacks), it should declare
`security.nesting: "true"` in `base.config`:

```yaml
base:
  image: ubuntu:24.04
  config:
    security.nesting: "true"
```

At anypoint it is even a possibility that Docker in Incus might be required, you MUST read the full guide on platform-specific issues and networking paths:

```
read_file("@digital-twin-universe:docs/docker-in-incus.md")
```

The `docker-in-incus` profile can be used to verify the setup works before
attempting more complex profiles.

### Networking: Docker + Incus conflict (WSL2)

The most common blocker. Docker sets the kernel's iptables FORWARD chain to DROP, which blocks Incus bridge traffic.

**Symptoms:** `apt-get update` fails inside containers, containers can't reach any external hosts.

**Fix (permanent, one-time):**
```bash
echo '{"ip-forward-no-drop": true}' | sudo tee /etc/docker/daemon.json
wsl --shutdown   # from PowerShell, then restart WSL
```

If networking still fails after the Docker fix, make sure all services and WSL was properly restarted.

### Incus permissions

**Symptom:** `You don't have the needed permissions to talk to the incus daemon`

```bash
sudo usermod -aG incus-admin $USER
newgrp incus-admin
```

Note: `newgrp` doesn't propagate to existing subprocesses. If running from within an Amplifier session, you may need to restart the terminal entirely.

### CLI argument parsing with `--var`

**Symptom:** `Got unexpected extra arguments` when passing `--var` with subshell expansion.

The JSON output from subshell commands gets expanded as multiple arguments. Extract just the value:
```bash
# Wrong:
--var GITEA_TOKEN=$(amplifier-gitea token <id>)

# Right:
--var GITEA_TOKEN=$(amplifier-gitea token <id> | jq -r .token)
```

### General reference

| Problem | Fix |
|---------|-----|
| `launch` hangs on provisioning | Usually a networking issue. Fix Docker/Incus networking first, then retry. Check container state with `incus list`. |
| `Server version: unreachable` from `incus version` | Your shell doesn't have the `incus-admin` group. Run `newgrp incus-admin` or log out and back in. |
| Provisioning fails with `command not found` | The provisioned tool isn't installed yet at that stage. Check profile provisioning order. |
| Amplifier inside container extremely slow | May hang on `Loading foundation`. Check container networking and compute allocation. |
| `Environment not found` for a previously created env | The Incus container was stopped or removed externally. Create a fresh environment. |
