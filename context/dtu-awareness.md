# Digital Twin Universe Environments

You have access to `amplifier-digital-twin`, a CLI for on-demand isolated environments stood up from declarative profiles. Use it to simulate real-world deployment scenarios so code can be tested as if actually deployed.

## When to Use

- User needs an isolated environment to test code in a realistic deployment setting
- User wants to launch a simulated Amplifier user environment or web UI
- User needs an ephemeral container with DNS rewriting, API passthrough, or port forwarding

## How to Use

Please load the `digital-twin-universe` skill. It will give you the necessary installation instructions, CLI documentation, profile reference, and troubleshooting:

```
load_skill(skill_name="digital-twin-universe")
```

## Prerequisites

- [Incus](https://linuxcontainers.org/incus/) installed and running (see install guide below)
- [Docker Engine](https://docs.docker.com/engine/install/) — required for profiles using Gitea repos or mock service sidecars (see install guide below)
- `amplifier-digital-twin` CLI installed (`uv tool install git+https://github.com/microsoft/amplifier-bundle-digital-twin-universe@main`)

## Installing Incus

If the user doesn't have Incus installed, walk them through the platform-specific steps in the install guide. After installation, always run the verification steps to confirm it works.
If their system is not in the documentation, go to the actual documentation at https://linuxcontainers.org/incus/docs/main/installing/

```
read_file("@digital-twin-universe:docs/installing-incus.md")
```

After installing, make sure to test to see if its working as expected.

## Installing Docker

Docker is only needed for profiles that use Gitea repos or mock service sidecars. Check if it's already installed with `docker version`. 
You should encourage the user to install Docker since some circumstances will require it.
If it's not installed, walk the user through the install guide:

```
read_file("@digital-twin-universe:docs/installing-docker.md")
```

After installing, make sure to test to see if its working as expected.


## Quick Command Reference

| Command | Purpose |
|---------|---------|
| `amplifier-digital-twin launch <profile>` | Launch an environment from a profile |
| `amplifier-digital-twin exec <id> [-- <cmd>]` | Run a command or open a shell inside an environment |
| `amplifier-digital-twin check-readiness <id>` | Run readiness checks (exit 0 = ready, 1 = not ready) |
| `amplifier-digital-twin status <id>` | Check environment status |
| `amplifier-digital-twin list` | List all managed environments |
| `amplifier-digital-twin destroy <id>` | Tear down an environment |

All commands output JSON to stdout.

## Example Profiles

- `amplifier-user-sim` — simulates an Amplifier user environment with LLM passthrough and Gitea repos
- `amplifier-chat` — launches a browser-accessible amplifier-chat UI on `http://localhost:8410/chat/`

Read example profiles:
```
read_file("@digital-twin-universe:profiles/amplifier-user-sim.yaml")
read_file("@digital-twin-universe:profiles/amplifier-chat.yaml")
```
