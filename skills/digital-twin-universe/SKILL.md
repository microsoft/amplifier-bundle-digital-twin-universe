---
name: digital-twin-universe
description: Launch and manage isolated, realistic environments (Digital Twin Universe) to test code beyond "tests pass on my machine" and simulate end-user experiences. Debug networking/provisioning as needed. Triggers on digital twin, DTU, isolated environment, simulation environment, amplifier-digital-twin, incus container, profile launch, test in realistic environment, deploy simulation. ALWAYS use this skill whenever a "Digital Twin" is mentioned!
user-invocable: true
visibility:
  priority: 5
  summary: Launch and manage isolated Digital Twin Universe environments to test code as if deployed; ALWAYS use when a Digital Twin/DTU is mentioned.
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

**Use the correct Gitea URL/endpoint for the system.** When the profile uses
`url_rewrites` or `pypi_overrides`, pass the Gitea endpoint via
`--var GITEA_URL=...` (and `--var GITEA_TOKEN=...`). The right value depends
on where the DTU host and the Gitea container can reach each other.

## Documentation

You **must** load these files and refer to them as they contain necessary information on how to use the digital twin universe correctly.

For overview, quick start, installation, and feature list:

```
read_file("@digital-twin-universe:README.md")
```

For complete CLI reference with all flags and output schemas:

```
read_file("@digital-twin-universe:docs/api-reference.md")
```

For the full profile schema and field reference, sample profiles organized by bucket (`amplifier/`, `patterns/`, `tests/`, `community/`), and contribution guidelines:

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

## Shell Access

When giving the user a command for an interactive shell in a DTU for a user (without explicit flag preferences), default to giving them a command with the default visual-id so they know that they are in a DTU:

```bash
amplifier-digital-twin exec --visual-id "" <id>
```

`--visual-id ""` (the empty string sentinel) prepends `(dtu:<profile>)` in
blue to the prompt so the user can tell which DTU they are in. If the user
has several DTUs on the same profile, pass an explicit label so the prompts
remain distinct:

```bash
amplifier-digital-twin exec --visual-id testing-pr-42 <id>
```

**Important:** `--visual-id` always takes a value. The empty string `""`
means "use the profile name"; any non-empty value is used as the literal
label. Always quote the empty string -- `--visual-id ""`.

## Running Commands in a DTU

Every command path (`exec`, `exec --stream`, `provision.setup_cmds`,
`provision.update.cmds`, `readiness.command`, bare interactive) is wrapped
in `bash -lc`. The login shell sources `/etc/profile.d/dtu-env.sh`, which
puts `/root/.cargo/bin:/root/.local/bin` on PATH and exports passthrough
env vars.

**Write commands bare.** Anything from `uv tool install` (`amplifier`,
`uv`, etc.) resolves without help:

```bash
amplifier-digital-twin exec <id> -- amplifier --version
```

Do NOT add any of the following — all redundant, all dead weight from older profiles:

- `bash -lc '...'` wrapping (double-wraps).
- `bash -c 'export PATH="/root/.local/bin:$PATH" && cmd'`.
- `PATH=/root/.local/bin:$PATH cmd` prefix on `readiness.command`.
- Hardcoded `/root/.local/bin/<tool>` paths — use bare commands or
  `command -v <tool>` for existence checks.
- `export PATH="/root/.local/bin:$PATH"` at the top of heredoc
  `setup_cmds` blocks.

Use `bash -c` only when you need a shell construct (pipes, redirects,
`&&`/`||`, expansion) in a single command. Do not use `bash -lc` — the
outer wrap already provides the login shell.

## File Transfer

`file-push` and `file-pull` move files between the host and a running DTU
without going through `exec`. Both commands accept multiple sources before
the destination.

```bash
# Single file
amplifier-digital-twin file-push <id> ./config.yaml /root/config.yaml
amplifier-digital-twin file-pull <id> /var/log/app.log ./app.log

# Directory (auto-detected; -r not required)
amplifier-digital-twin file-push <id> ./data/ /root/app/
amplifier-digital-twin file-pull <id> /root/results/ ./
```

`-r/--recursive` defaults to **off** for both commands. Directory sources
are **auto-detected** and transferred recursively regardless of the flag.
Leave `-r` off for single-file or multi-file transfers: on push it changes
the destination semantics (the destination is treated as a parent directory
and each file lands at `<destination>/<filename>`), and on pull it changes
symlink handling (with `-r` a symlink source is recreated as a symlink
instead of being dereferenced).

When pushing a directory, the destination is treated as the **parent
directory** and the source's basename is preserved inside it (`cp -r`
convention). Pushing `./data/` to
`/root/app/` lands files at `/root/app/data/...`. To put contents directly
at `/root/app/data/`, push to `/root/app/` and let the basename land
naturally (do not push to `/root/app/data/` -- that produces
`/root/app/data/data/...`).

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
read_file("@digital-twin-universe:profiles/tests/amplifier-user-sim.yaml")
read_file("@digital-twin-universe:profiles/amplifier/amplifier-chat.yaml")
read_file("@digital-twin-universe:profiles/amplifier/amplifier-standalone.yaml")
read_file("@digital-twin-universe:profiles/patterns/private-github-repo.yaml")
read_file("@digital-twin-universe:profiles/tests/docker-in-incus.yaml")
```

The `amplifier-standalone` profile is a standalone Amplifier user environment
with the foundation bundle composed onto every session. Use it when the user
wants to `exec` in and run interactive `amplifier` sessions immediately, with
no extra services or UI on top.

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

### Destroy-what-you-created (default posture)

**Every `launch` you perform pairs with one of these two outcomes before you
consider the work finished:**

1. **Destroy it.** Once the work that needed the instance completes or fails,
   `destroy` the specific `id` you launched. This is the default outcome for
   the overwhelming majority of DTU usage (verification runs, one-off tests,
   throwaway exploration).
2. **Explicit handoff.** If the instance must outlive your session (e.g. the
   user wants to keep exploring it interactively), your final report to the
   user or caller MUST name the exact instance `id` and state who now owns
   tearing it down. A handoff that doesn't name an owner is not a handoff --
   it's an orphan.

**"If you are unsure, leave it" is no longer an acceptable default.** An
instance nobody destroys and nobody owns does not get cleaned up later --
there is no reaper (see below). Stateless, repeated delegations that each
independently default to "leave it" is exactly how a machine ends up with
dozens of forgotten containers: no single delegation looks wrong in
isolation, but nothing ever converges on zero. If you are genuinely unsure
whether an instance is still needed, that uncertainty itself must be surfaced
explicitly in your report (naming the `id`) rather than silently resolved by
leaving the container running.

### Count before you launch

Before launching a new instance, run `amplifier-digital-twin list` and look
at how many DTU environments already exist. If a substantial number are
already running, reconsider whether you actually need another one --
especially before a batch or loop that will launch more than one. A pile of
existing instances is a signal that cleanup isn't keeping pace with launches,
not a reason to add to the pile.

The CLI enforces a hard ceiling on top of this judgment call: `launch`
refuses (non-zero exit) once live DTU instances reach `--max-instances`
(default 15, override via `--max-instances` or `AMPLIFIER_DTU_MAX_INSTANCES`,
`0` = unlimited). See [api-reference.md](../../docs/api-reference.md) for the
full flag reference. Treat a refusal from this guard as a real signal to
destroy unneeded instances or use an explicit override -- not as an obstacle
to route around reflexively.

### No TTL, no reaper -- ever

**Nothing in this system ever automatically destroys an instance.** There is
no time-to-live, no idle timeout, no background sweep that reclaims
forgotten containers. An instance launched and never destroyed will sit
there indefinitely, consuming disk, memory, and CPU, until a human or agent
explicitly runs `destroy` (or deletes the underlying Incus container
directly). Teardown is -- and will remain -- entirely the launching caller's
responsibility. Do not assume "it'll get cleaned up eventually."

### Resource limits

Profiles can (and, for anything beyond a quick throwaway check, should) set
per-instance resource limits via `base.config` (`limits.memory`,
`limits.cpu`). This bounds how much damage a single misbehaving or
long-lived instance can do to the host even if cleanup is delayed. See
[profiles.md](../../docs/profiles.md#base) for the schema and examples. Resource
limits are a mitigation, not a substitute for actually destroying instances
you no longer need.


### Docker Inside a Digital Twin Universe Environment (pre-flight)

DTU launches enable `security.nesting=true` by default, so profiles that run
Docker inside the Incus container (e.g. spawning worker containers, running
Docker Compose stacks) do not need to set it explicitly. A profile can opt out
by setting `security.nesting: "false"` in `base.config` if isolation matters
more than Docker support.

At any point that Docker in Incus might be required, you MUST read the full
guide on platform-specific issues and networking paths:

```
read_file("@digital-twin-universe:docs/docker-in-incus.md")
```

The `docker-in-incus` profile can be used to verify the setup works before
attempting more complex profiles.


## Troubleshooting

For any error, unexpected behavior, or environment issue, read the troubleshooting reference:

```
read_file("@digital-twin-universe:docs/troubleshooting.md")
```

If the behavior contradicts the docs, check what changed between versions:

```
read_file("@digital-twin-universe:docs/CHANGELOG.md")
```
