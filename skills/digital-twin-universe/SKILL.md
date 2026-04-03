---
name: digital-twin-universe
description: Use when the user needs an isolated, realistic deployment environment to test code beyond "tests pass on my machine." Covers launching environments from declarative profiles, executing commands inside them, managing their lifecycle, debugging networking or provisioning issues, and working with Incus containers. Also use when the user wants to simulate an end-user experience (e.g. Amplifier CLI, web UIs) without touching production infrastructure. Triggers on digital twin, DTU, isolated environment, simulation environment, amplifier-digital-twin, incus container, profile launch, test in realistic environment, deploy simulation.
user-invocable: true
---

# Digital Twin Universe Environments

`amplifier-digital-twin` is a CLI for on-demand, isolated environments launched from declarative profiles. All commands output JSON to stdout.

## Prerequisites Check

Before any DTU operation, verify the environment:

```bash
# 1. Is the CLI installed?
which amplifier-digital-twin

# 2. Is Incus available and running?
which incus && incus version && echo "Incus OK" || echo "Incus NOT available"
```

If `amplifier-digital-twin` is not found:
```bash
uv tool install git+https://github.com/microsoft/amplifier-bundle-digital-twin-universe@main
```

If Incus is not installed or running, read the install guide for platform-specific steps and verification:
```
read_file("@digital-twin-universe:docs/installing-incus.md")
```

If Docker is needed (profiles with Gitea or mock services) and `docker version` fails:
```
read_file("@digital-twin-universe:docs/installing-docker.md")
```

**If prerequisites are missing, report clearly and stop. Do not attempt workarounds.**

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

## Example Profiles

```
read_file("@digital-twin-universe:profiles/amplifier-user-sim.yaml")
read_file("@digital-twin-universe:profiles/amplifier-chat.yaml")
```

## Troubleshooting

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
