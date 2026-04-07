# Digital Twin Universe Environments

You have access to `amplifier-digital-twin`, a CLI for on-demand isolated environments stood up from declarative profiles. Use it to simulate real-world deployment scenarios so code can be tested as if actually deployed.

## When to Use

- User needs an isolated environment to test code in a realistic deployment setting
- User wants to launch a simulated Amplifier user environment or web UI
- User needs an ephemeral container with DNS rewriting, API passthrough, or port forwarding
- User has local repos that need to be tested as if they were already published (use the Gitea skill for this)
- And much more, load the skill to discover that!!

## How to Use

You MUST load the `digital-twin-universe` skill as a FIRST STEP.
It will tell you what the necessary prerequisites, installation instructions, CLI documentation, sample profiles, profile reference, and troubleshooting:

```
load_skill(skill_name="digital-twin-universe")
```

If the scenario involves local code in Gitea, load the `gitea` skill:

```
load_skill(skill_name="gitea")
```

If you DO NOT load these skills, you will FAIL and let the user down :'(
