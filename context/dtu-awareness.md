# Digital Twin Universe Environments

You have access to `amplifier-digital-twin`, a CLI for on-demand isolated environments stood up from declarative profiles. Use it to simulate real-world deployment scenarios so code can be tested as if actually deployed.

## When to Use

- User needs an isolated environment to test code in a realistic deployment setting
- User wants to launch a simulated Amplifier user environment or web UI
- User needs an ephemeral container with DNS rewriting, API passthrough, or port forwarding

## Prerequisites

- [Incus](https://linuxcontainers.org/incus/) installed and running
- `amplifier-digital-twin` CLI installed (`uv tool install git+https://github.com/microsoft/amplifier-bundle-digital-twin-universe@main`)

## Quick Command Reference

| Command | Purpose |
|---------|---------|
| `amplifier-digital-twin launch <profile>` | Launch an environment from a profile |
| `amplifier-digital-twin exec <id> [-- <cmd>]` | Run a command or open a shell inside an environment |
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

## Full Reference

Load the `digital-twin-universe` skill for complete CLI documentation, profile reference, and troubleshooting:

```
load_skill(skill_name="digital-twin-universe")
```
