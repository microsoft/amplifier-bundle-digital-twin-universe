# API Reference

This reference documents v0.3.0. For previous versions, check out the corresponding git tag or commit (`git checkout v<version>`). When upgrading across a breaking change, review the diff between versions to find the migration steps. The source can be cloned from https://github.com/microsoft/amplifier-bundle-digital-twin-universe

CLI: `amplifier-digital-twin`

All commands return JSON to stdout unless noted otherwise.


## Lifecycle

> **Instance lifecycle.** `launch` creates an unmanaged, long-lived Incus
> container. There is no reaper and no TTL -- nothing here ever destroys an
> instance automatically. Teardown (`destroy`) is always the caller's
> responsibility. See [profiles.md#instance-lifecycle](profiles.md#instance-lifecycle)
> for the full explanation and [`launch --max-instances`](#launch) for the
> guard that bounds how many can accumulate.

### `launch`

Launch a new Digital Twin Universe from a profile. Creates an Incus container,
starts mock service Docker sidecars (if `mock_services` is configured), sets up the
HTTPS rewriting proxy (if `url_rewrites` or `mock_services` require it), runs
provisioning, and returns connection details.

```bash
amplifier-digital-twin launch <profile> \
  [--var KEY=VALUE ...] \
  [--name my-env] \
  [--hostname my-project] \
  [--max-instances N]
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

`--max-instances N` (optional)
  Refuse to launch if the number of currently live DTU-managed instances
  (the same population `list` returns) is already `>= N`. On refusal, `launch`
  exits non-zero *before* creating any Incus container, and prints an error
  naming the current instance count, the cap that was hit, and how to
  override it. `N = 0` means unlimited (the guard is disabled).

  Cap resolution order (first one set wins):
  1. `--max-instances` flag, if passed
  2. `AMPLIFIER_DTU_MAX_INSTANCES` environment variable, if set
  3. Default: `15`

  This exists because nothing else in the system ever destroys an instance
  automatically -- see [profiles.md#instance-lifecycle](profiles.md#instance-lifecycle).
  Without a cap, repeated or looped launches (e.g. from an automated
  pipeline or a delegated agent that doesn't itself tear down after every
  run) can silently accumulate containers until the host runs out of disk,
  memory, or CPU. The cap is a backstop, not a substitute for destroying
  instances you no longer need -- raising or disabling it does not make the
  underlying resource pressure go away.

  ```bash
  # Use a higher cap for a workload that legitimately needs more instances
  amplifier-digital-twin launch my-profile --max-instances 30

  # Disable the guard entirely
  amplifier-digital-twin launch my-profile --max-instances 0

  # Same override via environment variable (useful for CI/pipelines)
  AMPLIFIER_DTU_MAX_INSTANCES=0 amplifier-digital-twin launch my-profile
  ```

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
amplifier-digital-twin exec [--stream] [--timeout <seconds>] <id> [-- <command> [args...]]
```

`<id>` (required)
  Environment ID.

`<command>` (optional, after `--`)
  Command to run. If omitted, starts an interactive shell (`/bin/bash`).

`--stream` (optional)
  Stream stdout and stderr in real-time instead of buffering and returning
  JSON. The process exit code becomes the CLI exit code. Useful for
  long-running commands where you want to see output as it is produced.

`--timeout <seconds>` (optional, default: `600`)
  Maximum number of seconds to wait for a streamed command to complete.
  Pass `--timeout none` to disable the timeout entirely. Only applies in
  `--stream` mode; interactive and JSON modes are unaffected.

`--visual-id [LABEL]` (optional)
  Prepend a `(dtu:<label>)` prefix to the interactive shell prompt so
  you can tell DTU sessions apart when you have several open. This flag
  always takes a value; the value controls the label:

  - `--visual-id ""` (empty string) uses the DTU's profile name, e.g.
    `(dtu:amplifier-user-sim)`. This is the common case.
  - `--visual-id testing-pr-42` uses the given string, e.g.
    `(dtu:testing-pr-42)`. Useful when multiple DTUs share a profile.

  Value is restricted to `[A-Za-z0-9._:/-]` and capped at 40 characters.
  Interactive mode only -- accepted but ignored in JSON and `--stream` modes.

Exec operates in three modes depending on the arguments:

| Mode | Invocation | PTY | Output |
|------|-----------|-----|--------|
| **Interactive** | `exec <id>` (no command) | Yes (`--force-interactive`) | Live terminal |
| **JSON** | `exec <id> -- <command>` | No | JSON with stdout/stderr |
| **Stream** | `exec --stream <id> -- <command>` | No (unless caller has one) | Raw passthrough |

```bash
# Interactive shell (live terminal, PTY allocated)
amplifier-digital-twin exec dtu-a1b2c3d4

# Run a single command (returns JSON, no PTY)
amplifier-digital-twin exec dtu-a1b2c3d4 -- amplifier --version

# Stream output in real-time (no PTY unless caller provides one)
amplifier-digital-twin exec --stream dtu-a1b2c3d4 -- amplifier run "test prompt"

# Interactive shell with a blue (dtu:<profile>) prompt prefix
amplifier-digital-twin exec --visual-id "" dtu-a1b2c3d4

# Same, but with a custom label instead of the profile name
amplifier-digital-twin exec --visual-id testing-pr-42 dtu-a1b2c3d4
```

#### Visual session identifier

`--visual-id` injects a blue `(dtu:<id>)` prefix into the interactive shell's
prompt so you can tell multiple DTU sessions apart. It is **off by default**
and only affects interactive mode.

The flag always takes a value. The value controls the label:

- `--visual-id ""` (empty string sentinel) uses the DTU's profile name, e.g.
  `(dtu:amplifier-user-sim)`. Always quote the empty string so the shell
  passes it through.
- `--visual-id testing-pr-42` uses the string you pass, e.g.
  `(dtu:testing-pr-42)`. Useful when several DTUs share a profile.
- Values are restricted to `[A-Za-z0-9._:/-]` and capped at 40 characters.
- In JSON and `--stream` modes the flag is accepted but ignored -- there is
  no prompt to prefix.

Under the hood, every `exec` surface launches the same `bash -l` login shell
(`exec -- <cmd>` uses `bash -lc`, bare interactive uses `bash -l`,
`--visual-id` uses `bash -l` with `DTU_VISUAL_ID=<label>` forwarded via
`incus exec --env`). A static `/etc/profile.d/dtu-visual-id.sh` script
written at launch picks up `DTU_VISUAL_ID` on interactive shells and chains
a `_dtu_apply_prompt` entry into `PROMPT_COMMAND` so the prefix re-applies
each prompt redraw even if `~/.bashrc` resets `PS1` afterward.

Without a command, attaches a terminal to the container with a PTY.
Exit code comes from the shell when you exit.

With a command after `--`, runs it and returns JSON by default:

Returns (default, no `--stream`):

```json
{
  "id": "dtu-a1b2c3d4",
  "command": "amplifier --version",
  "exit_code": 0,
  "stdout": "amplifier 1.3.0\n",
  "stderr": ""
}
```

With `--stream`, stdout and stderr are passed through as raw text (no JSON
envelope). The CLI exit code is the command's exit code.

#### Internal shell wrapping

When a command is provided (JSON or stream mode), it is internally wrapped in
`bash -lc "<command>"`. This means:

- **Do not add `bash` yourself.** `exec <id> -- bash` creates a nested
  `bash -lc "bash"` that hangs waiting for input.
- **Do not use `bash -c`.** `exec <id> -- bash -c '...'` double-nests shells
  unnecessarily (though it does work).
- **Shell features work in single commands.** Pipes, redirects, and variable
  expansion work: `exec <id> -- ls /tmp | head -5`.
- **Chained commands (`&&`, `||`, `;`) need an explicit `bash -c` wrapper.**
  Passing them as a single quoted string like `exec <id> -- "cmd1 && cmd2"`
  sends the whole string as one token. `shlex.join` then single-quotes it
  (because `&&` is a shell metacharacter), so the inner `bash -lc` sees
  `'cmd1 && cmd2'` as one word and tries to execute it as an executable
  filename. Wrap with `bash -c` to chain:
  `exec <id> -- bash -c 'cmd1 && cmd2'`.
- **The `--` separator is required** when command arguments contain flags.
  Without `--`, flags like `--version` are consumed by the CLI itself.

```bash
# GOOD -- flags passed to the command
amplifier-digital-twin exec dtu-a1b2c3d4 -- myapp --version

# BAD -- --version consumed by the CLI, fails with "No such option"
amplifier-digital-twin exec dtu-a1b2c3d4 myapp --version

# BAD -- nested bash, hangs
amplifier-digital-twin exec dtu-a1b2c3d4 -- bash

# UNNECESSARY -- double-nesting, but works
amplifier-digital-twin exec dtu-a1b2c3d4 -- bash -c 'myapp --version'
```

#### TUI and interactive applications

JSON and stream modes do **not** allocate a PTY inside the container.
Applications that require a terminal (curses, Ink, Ratatui, etc.) will fail
or render incorrectly in these modes.

For interactive or TUI apps, use **interactive mode** (no command) and launch
the app from within the shell:

```bash
# 1. Get an interactive shell with PTY
amplifier-digital-twin exec dtu-a1b2c3d4
# 2. Now inside the container, run your app
root@dtu-a1b2c3d4:~# myapp
```

When driving apps programmatically via a PTY harness, spawn exec in interactive
mode, wait for the shell prompt, then send keystrokes to launch the app.

Use `--stream` when:
- Running long-lived commands where you want to see progress as it happens
  (e.g. `amplifier run`, install scripts, build steps)
- Piping output to another command (`exec --stream ... | grep ...`)
- Watching logs or interactive-style output from inside the container

Use the default (no `--stream`) when:
- Parsing the result programmatically (the JSON envelope gives you structured
  `exit_code`, `stdout`, and `stderr` fields)
- Running from scripts or recipes that inspect the output
- Running short commands where buffering doesn't matter
  (e.g. `amplifier --version`, `cat /etc/os-release`)


### `check-readiness`

Run readiness checks for an environment. Checks are defined in the profile's
`readiness` section and evaluated inside the container on each invocation.
Host-side verification of `access.ports` is included by default. This command
is stateless -- the caller owns the polling loop.

```bash
amplifier-digital-twin check-readiness <id> [--skip-access-check]
```

`<id>` (required)
  Environment ID.

`--skip-access-check` (optional)
  Skip host-side verification of `access.ports`. Only the profile's
  `readiness` checks are evaluated.

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

#### Profile snapshot

`update` reads the profile from a snapshot written inside the container at
launch time (`/opt/dtu/profile.yaml`), not from the host filesystem. This
means:

- `update` succeeds even if the original host profile file has been moved,
  renamed, or deleted since launch.
- Edits made to the host profile after launch are **not** picked up on
  update. The container owns its own profile.
- The snapshot is the raw pre-substitution YAML; `--var` values supplied to
  `update` are applied freshly at update time.

To inspect the snapshot:

```bash
amplifier-digital-twin exec <id> -- cat /opt/dtu/profile.yaml
```

Containers launched before this snapshot feature existed fall back to
host-side profile resolution by name and print a warning to stderr.


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
  Destination path inside the container.

  - **Single-file or multi-file push:** treated as a file path (for a single
    source) or a parent directory (for multiple sources).
  - **Directory push (`-r`):** `<container_path>` is treated as the **parent
    directory**, and the source's basename is preserved inside it. Pushing
    `./data/` to `/root/app/data/` lands files at `/root/app/data/data/...`,
    not directly at `/root/app/data/`. To land contents directly, push to
    the parent and let the basename land naturally
    (e.g. `./data/` → `/root/app/` produces `/root/app/data/...`).

`-r/--recursive` (default: off)
  Directory sources are **auto-detected** and pushed recursively regardless
  of this flag (each lands at `<container_path>/<basename>`, `cp -r`
  convention). The flag's only remaining effect is on **file-only** pushes:
  it treats the destination as a parent directory and the source is created
  inside it (e.g. `file-push -r foo.txt /dest/file.txt` creates
  `/dest/file.txt/foo.txt`, where `file.txt` becomes a directory). Leave it
  off (the default) for exact-path single-file or multi-file pushes.

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
# Single file -> file path
amplifier-digital-twin file-push dtu-a1b2c3d4 ./config.yaml /root/config.yaml

# Multiple files -> directory (no -r needed)
amplifier-digital-twin file-push dtu-a1b2c3d4 a.yaml b.yaml /root/data/

# Directory tree -- basename preserved inside dest.
# ./data/ lands at /root/app/data, so use the parent as dest.
amplifier-digital-twin file-push -r dtu-a1b2c3d4 ./data/ /root/app/
```

Returns:

```json
{
  "instance_id": "dtu-a1b2c3d4",
  "sources": ["./config.yaml"],
  "dest": "/root/config.yaml",
  "recursive": false
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

`-r/--recursive` (default: off)
  Remote directory sources are **auto-detected** and pulled recursively
  regardless of this flag (each lands at `<local_path>/<basename>`, `cp -r`
  convention). For file-only pulls the flag is forwarded to
  `incus file pull --recursive`; regular files are unaffected, but it
  changes symlink handling: with the flag a symlink source is recreated as
  a symlink instead of being dereferenced.

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
