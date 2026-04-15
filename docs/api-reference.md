# API Reference

CLI: `amplifier-digital-twin`

All commands return JSON to stdout unless noted otherwise.


## Lifecycle

### `launch`

Launch a new Digital Twin Universe from a profile. Creates an Incus container,
starts mock service Docker sidecars (if `mock_services` is configured), sets up the
HTTPS rewriting proxy (if `url_rewrites` or `mock_services` require it), runs
provisioning, and returns connection details.

```bash
amplifier-digital-twin launch <profile> \
  [--var KEY=VALUE ...] \
  [--name my-env] \
  [--hostname my-project]
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

`--hostname` (optional)
  Register a `.local` mDNS hostname via Avahi. The `.local` suffix is appended
  automatically (e.g. `--hostname my-app` registers `my-app.local`).
  Access URLs will use the hostname instead of `localhost`.
  Overrides any `hostname` set in the profile's `access` section.
  Requires `avahi-utils`. If unavailable, silently skipped.
  See [profiles.md](profiles.md#hostname-support) for platform details.

Returns (example):

```json
{
  "id": "dtu-a1b2c3d4",
  "name": "dtu-a1b2c3d4",
  "profile": "amplifier-chat",
  "status": "running",
  "created_at": "2026-03-23T16:00:00Z",
  "hostname": "amplifier-chat.local",
  "container_ip": "10.231.68.42",
  "access": [
    {"label": "Chat UI", "url": "http://localhost:8410/chat/", "mdns_url": "http://amplifier-chat.local:8410/chat/"}
  ],
  "mock_services": [
    {
      "name": "slack",
      "container_id": "a1b2c3d4e5f6",
      "host_port": 38421,
      "domains": ["api.slack.com", "slack.com"]
    }
  ],
  "info": [
    "Readiness checks configured. Poll with: amplifier-digital-twin check-readiness dtu-a1b2c3d4"
  ]
}
```

Optional fields:

- `hostname` -- present when a `.local` hostname was successfully registered via Avahi.
  The value is the fully qualified name (e.g. `amplifier-chat.local`).
- `access[].mdns_url` -- present on each access entry when a hostname is registered.
  The `url` field always uses `localhost`; `mdns_url` uses the `.local` hostname.
- `container_ip` and `access` -- present when the profile defines `access.ports`
- `mock_services` -- present when the profile defines `mock_services`. Each entry includes
  the mock service name, Docker container ID, ephemeral host port, and intercepted domains.
- `info` -- list of guidance strings. Contains a readiness hint when the profile defines
  `readiness` checks. Empty list when there are none.

See [profiles.md](profiles.md) for the `access`, `base.config`, `mock_services`, and `readiness` schemas.


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


### `check-readiness`

Run readiness checks for an environment. Checks are defined in the profile's
`readiness` section and evaluated inside the container on each invocation.
This command is stateless -- the caller owns the polling loop.

```bash
amplifier-digital-twin check-readiness <id>
```

`<id>` (required)
  Environment ID.

Exit codes:
- `0` -- all checks passed (ready), or no readiness checks configured
- `1` -- one or more checks failed (not ready)
- `2` -- error (bad environment ID, container gone, etc.)

Returns (not ready):

```json
{
  "ready": false,
  "message": "1/2 checks passed",
  "checks": {
    "chat-server": { "passed": true },
    "amplifierd-ready": { "passed": false, "message": "HTTP 503" }
  }
}
```

Returns (ready):

```json
{
  "ready": true,
  "message": "all checks passed"
}
```

Returns (no readiness section in profile):

```json
{
  "ready": null,
  "message": "profile has no readiness checks"
}
```

The `checks` object is only included when `ready` is `false`. Per-check
`message` contains the last failure reason (HTTP status, connection error,
exit code + stderr, or body mismatch).

Polling example:

```bash
while ! amplifier-digital-twin check-readiness dtu-a1b2c3d4 \
    | jq -e '.ready'; do
  sleep 3
done
```


### `update`

Update provisioned software in a running environment without destroying it.
Re-runs the profile's `update` section: optionally refreshes PyPI overrides
(rebuilds and re-pushes wheels), then executes the update commands. Readiness
checks are re-run automatically unless `--skip-readiness` is set.

```bash
amplifier-digital-twin update <id> \
  [--var KEY=VALUE ...] \
  [--skip-readiness]
```

`<id>` (required)
  Environment ID.

`--var` (optional, repeatable)
  Variable substitution for `${VAR}` references in the profile (same as
  `launch`). Required when the profile's `update` or `pypi_overrides` sections
  reference variables (e.g. `GITEA_URL`, `GITEA_TOKEN`).

`--skip-readiness` (optional)
  Skip readiness checks after update. By default, if the profile defines
  readiness checks they are re-run after the update commands complete.

Returns:

```json
{
  "id": "dtu-a1b2c3d4",
  "profile": "amplifier-user-sim",
  "status": "updated",
  "pypi_refreshed": true,
  "cmds_run": 1,
  "readiness": {
    "ready": true,
    "message": "all checks passed"
  }
}
```

Optional fields:

- `readiness` -- present unless `--skip-readiness` was used. Contains the
  result of running readiness checks after the update.
- `pypi_refreshed` -- `true` when PyPI overrides were rebuilt and re-pushed.

Requires the profile to define an `update` section. See
[profiles.md](profiles.md#update) for the schema.


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
  "created_at": "2026-03-23T16:00:00Z",
  "hostname": "amplifier-user-sim.local",
  "access": [
    {"label": "Chat UI", "url": "http://localhost:8410/chat/", "mdns_url": "http://amplifier-user-sim.local:8410/chat/"}
  ]
}
```

`status` is the Incus container state (e.g. `"running"`, `"stopped"`).

Optional fields:

- `hostname` -- present when a `.local` hostname was registered at launch time.
- `access` -- present when the profile defines `access.ports`. Same shape as `launch`
  output: `url` always uses `localhost`, `mdns_url` present when hostname is registered.


### `list`

List all environments managed by this tool.

> **Machine-wide scope.** This command returns **every** DTU environment on the
> machine, not just ones created by your session. Other users or concurrent
> Amplifier sessions may have running environments. Use `id` and `created_at`
> to identify the instances you own before taking action on them.

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
    "created_at": "2026-03-23T16:00:00Z",
    "hostname": "amplifier-user-sim.local",
    "access": [
      {"label": "Chat UI", "url": "http://localhost:8410/chat/", "mdns_url": "http://amplifier-user-sim.local:8410/chat/"}
    ]
  }
]
```

Returns an empty array `[]` when no environments exist.

`status` is the Incus container state (e.g. `"running"`, `"stopped"`).
`hostname` is present when a `.local` hostname was registered at launch time.
`access` is present when the profile defines `access.ports`.


## File Operations

### `file-push`

Push files from the host into an instance.

```bash
amplifier-digital-twin file-push <id> <source>... <container_path> [flags]
```

`<id>` (required)
  Environment ID.

`<source>...` (required)
  One or more local file or directory paths.

`<container_path>` (required)
  Destination path inside the container. When pushing multiple sources, this
  must be an existing directory.

`-r/--recursive` (default: on)
  Recursively transfer directories. Use `-R` or `--no-recursive` to disable.

`-p/--create-dirs` (default: on)
  Create any intermediate directories necessary in the container.
  Use `-P` or `--no-create-dirs` to disable.

`--mode`
  Set file permissions on push (e.g. `0644`).

`--uid`
  Set file UID on push.

`--gid`
  Set file GID on push.

`--timeout`
  Timeout in seconds (default: 120).

```bash
# Single file
amplifier-digital-twin file-push dtu-a1b2c3d4 ./config.yaml /root/config.yaml

# Multiple files into a directory
amplifier-digital-twin file-push dtu-a1b2c3d4 a.yaml b.yaml /root/data/

# Directory tree
amplifier-digital-twin file-push dtu-a1b2c3d4 ./data/ /root/app/data/

# Disable recursive (single files only)
amplifier-digital-twin file-push dtu-a1b2c3d4 -R ./file.txt /root/file.txt
```

Returns:

```json
{
  "instance_id": "dtu-a1b2c3d4",
  "sources": ["./config.yaml"],
  "dest": "/root/config.yaml",
  "recursive": true
}
```


### `file-pull`

Pull files from an instance to the host.

```bash
amplifier-digital-twin file-pull <id> <container_path>... <local_path> [flags]
```

`<id>` (required)
  Environment ID.

`<container_path>...` (required)
  One or more paths inside the container.

`<local_path>` (required)
  Destination path on the host. When pulling multiple sources, this must be
  an existing directory.

`-r/--recursive` (default: on)
  Recursively transfer directories. Use `-R` or `--no-recursive` to disable.

`-p/--create-dirs` (default: on)
  Create any intermediate directories necessary on the host.
  Use `-P` or `--no-create-dirs` to disable.

`--timeout`
  Timeout in seconds (default: 120).

```bash
# Single file
amplifier-digital-twin file-pull dtu-a1b2c3d4 /var/log/app.log ./app.log

# Directory
amplifier-digital-twin file-pull dtu-a1b2c3d4 /root/output/ ./results/
```

Returns:

```json
{
  "instance_id": "dtu-a1b2c3d4",
  "sources": ["/var/log/app.log"],
  "dest": "./app.log",
  "recursive": true
}
```


## Teardown

### `destroy`

Destroy an environment. Stops and removes any mock service Docker containers
associated with the environment, then stops and deletes the Incus container
and any associated storage.

> **Only destroy instances you created.** The `list` command returns all DTU
> environments on the machine. Other users or sessions may have running
> instances. Always destroy by specific `id` from your own `launch` output --
> never iterate `list` and destroy everything.

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
