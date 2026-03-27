# API Reference

CLI: `amplifier-digital-twin`

All commands return JSON to stdout unless noted otherwise.


## Lifecycle

### `launch`

Launch a new Digital Twin Universe from a profile. Creates an Incus container,
sets up the HTTPS rewriting proxy (if `url_rewrites` is configured), runs
provisioning, and returns connection details.

```bash
amplifier-digital-twin launch <profile> \
  [--var KEY=VALUE ...] \
  [--name my-env]
```

`<profile>` (required)
  Profile to launch. Accepts:
  - Absolute path: `/home/user/my-profile.yaml`
  - Relative path: `./profiles/my-profile.yaml`
  - Built-in name: `amplifier-user-sim` (resolved from `profiles/`)

`--var` (optional, repeatable)
  Variable substitution for `${VAR}` references in the profile.
  Example: `--var GITEA_URL=http://10.0.0.1:10110`

`--name` (optional)
  Human-readable name. Defaults to `dtu-<uuid8>`.

Returns:

```json
{
  "id": "dtu-a1b2c3d4",
  "name": "dtu-a1b2c3d4",
  "profile": "amplifier-user-sim",
  "status": "running",
  "created_at": "2026-03-23T16:00:00Z"
}
```


### `exec`

Execute a command or start an interactive shell inside a running environment.

```bash
amplifier-digital-twin exec <id> [-- <command> [args...]]
```

`<id>` (required)
  Environment ID.

`<command>` (optional, after `--`)
  Command to run. If omitted, starts an interactive shell (`/bin/bash`).

```bash
# Interactive shell (live terminal, not JSON)
amplifier-digital-twin exec dtu-a1b2c3d4

# Run a single command
amplifier-digital-twin exec dtu-a1b2c3d4 -- amplifier --version
```

Without a command, attaches a terminal to the container. 
Exit code comes from the shell when you exit.

With a command after `--`, runs it and returns JSON:

Returns:

```json
{
  "id": "dtu-a1b2c3d4",
  "command": "amplifier --version",
  "exit_code": 0,
  "stdout": "amplifier 1.3.0\n",
  "stderr": ""
}
```


### `status`

Check whether an environment exists and is running.

```bash
amplifier-digital-twin status <id>
```

`<id>` (required)
  Environment ID.

Returns:

```json
{
  "id": "dtu-a1b2c3d4",
  "profile": "amplifier-user-sim",
  "status": "running",
  "created_at": "2026-03-23T16:00:00Z"
}
```

`status` is the Incus container state (e.g. `"running"`, `"stopped"`).


### `list`

List all environments managed by this tool.

Environments are discovered via Incus instance config keys. During `launch`,
each container is tagged with `user.dtu.managed-by=amplifier-digital-twin`.
`list` queries Incus for instances with that key.

```bash
amplifier-digital-twin list
```

Returns:

```json
[
  {
    "id": "dtu-a1b2c3d4",
    "profile": "amplifier-user-sim",
    "status": "running",
    "created_at": "2026-03-23T16:00:00Z"
  }
]
```

Returns an empty array `[]` when no environments exist.

`status` is the Incus container state (e.g. `"running"`, `"stopped"`).


### `destroy`

Destroy an environment. Stops and deletes the Incus container and any associated storage.


```bash
amplifier-digital-twin destroy <id>
```

`<id>` (required)
  Environment ID.

Returns:

```json
{
  "id": "dtu-a1b2c3d4",
  "destroyed": true
}
```
