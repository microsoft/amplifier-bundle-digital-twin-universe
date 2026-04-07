# Digital Twin Universe Environments

You have access to Digital Twin Universe (DTU) capabilities for on-demand isolated environments stood up from declarative profiles. Use it to simulate real-world deployment scenarios so code can be tested as if actually deployed.

## When to Use

- User needs an isolated environment to test code in a realistic deployment setting
- User wants to launch a simulated Amplifier user environment or web UI
- User needs an ephemeral container with DNS rewriting, API passthrough, or port forwarding
- User has local repos that need to be tested as if they were already published

## How to Use

**ALWAYS delegate DTU work to the specialized agents.** Do NOT attempt to drive the `amplifier-digital-twin` CLI directly. The agents carry the full DTU knowledge in their own context, keeping your session lean. Default to passing full context so the agent has everything it needs.

Build a DTU profile for a project, launch and verify an environment (also handles Gitea setup for local repos):
```
delegate(agent="digital-twin-universe:dtu-profile-builder", instruction="<what the user needs>", context_depth="all", context_scope="full")
```

Verify a web UI running inside a DTU with a real browser:
```
delegate(agent="digital-twin-universe:dtu-browser-tester", instruction="<what the user needs>", context_depth="all", context_scope="full")
```

If the user's request doesn't match either agent (e.g. general DTU questions, installation help, troubleshooting), load the skill instead:
```
load_skill(skill_name="digital-twin-universe")
```
