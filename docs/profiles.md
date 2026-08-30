# Profile Reference

A profile can be launched by:

- **built-in name** — e.g. `amplifier-user-sim`. The built-in lookup searches
  `profiles/**/*.yaml` recursively across the bucket subdirectories. If two
  profiles in different buckets share a name the CLI raises an error; pass an
  explicit path to disambiguate.
- **relative path** to a YAML file
- **absolute path** to a YAML file

Sample profiles live in the [profiles/](../profiles/) directory, organized
into four buckets by intent. See [Sample profiles](#sample-profiles) below
and [Contributing a profile](#contributing-a-profile) for how to submit your
own.

> **Unknown fields warn.** Fields not listed in this reference are dropped
> and emit an `UnknownProfileFieldWarning`. A future release may turn these
> into hard errors instead of warnings.

## Variables

Profiles can reference launch-time variables with `${VAR_NAME}`.

```bash
amplifier-digital-twin launch amplifier-user-sim \
  --var GITEA_URL=http://localhost:10110 \
  --var GITEA_TOKEN=...
```

Current behavior:

- variable substitution is applied across all string values in the profile
- unresolved variables in `url_rewrites.rules[].target` cause proxy setup to be skipped
- unresolved variables in `pypi_overrides.packages[].wheel_from_git.repo` cause launch to fail
- `localhost` and `127.0.0.1` in launch variables are rewritten to the host gateway IP so the container can reach host-side services like Gitea

## `name`

Optional in practice. If omitted, the YAML filename stem is used.

```yaml
name: amplifier-user-sim
```

## `description`

Optional free-form text.

```yaml
description: Simulating an Amplifier user's experience
```

## `base`

Required.

```yaml
base:
  image: ubuntu:24.04
  config:                              # optional, merged on top of defaults
    limits.cpu: "4"
```

`base.image` (required) selects the container image.

`base.config` (optional) passes Incus container config flags at creation time.
Each key-value pair becomes an `incus launch --config key=value` argument. Use
this for Incus-level settings like `limits.cpu`, `limits.memory`, etc.

`security.nesting: "true"` is applied by default to every DTU launch (required
for running Docker and other container runtimes inside Incus). Profiles can
override it by setting it explicitly in `base.config`, including setting it to
`"false"` to opt out.

See [docs/docker-in-incus.md](docker-in-incus.md) for the Docker nesting use
case.

### Resource limits

Set per-instance CPU and memory ceilings via `base.config`, the same
mechanism as any other Incus config key:

```yaml
base:
  image: ubuntu:24.04
  config:
    limits.cpu: "2"          # number of vCPUs (Incus accepts a count or a range)
    limits.memory: "4GiB"    # Incus memory suffix syntax (e.g. 512MiB, 4GiB)
```

There is no default resource limit applied by the CLI beyond what the Incus
image profile itself sets -- an unbounded instance can consume as much
CPU/memory as the host has available. Setting `limits.cpu` / `limits.memory`
is cheap insurance against a single misbehaving or long-lived instance
starving the host, and is especially worth doing for anything launched
unattended (e.g. by an automated pipeline or a delegated agent) where a
human isn't watching resource usage in real time. See the [Instance
lifecycle](#instance-lifecycle) section below for why this matters given
there is no automatic cleanup.

### Instance lifecycle

**`launch` creates an unmanaged, long-lived Incus container.** Once created,
it keeps running -- consuming CPU, memory, and disk -- until something
explicitly destroys it. There is **no reaper and no TTL**: nothing in this
system ever automatically stops or deletes an instance based on age,
idleness, or any other signal. Teardown (`amplifier-digital-twin destroy
<id>`) is entirely the launching caller's responsibility, every time.

The CLI provides one enforcement mechanism to keep unbounded launching from
silently exhausting the host: `launch --max-instances N` (see
[api-reference.md](api-reference.md#launch)) refuses to create a new
instance once the number of live DTU-managed instances reaches `N`. This
bounds *how many* orphans can accumulate before launches start failing; it
does not destroy anything on your behalf. Combine it with the resource
limits above to bound how much damage each individual instance can do while
it's running.

## `url_rewrites`

Optional. When present and fully resolved, launch configures a mitmproxy-based
HTTPS proxy inside the environment and exports `HTTP_PROXY` / `HTTPS_PROXY`
for later provisioning commands and interactive use.

Loopback is exempted (`no_proxy` / `NO_PROXY` = `localhost,127.0.0.1,::1`), so
in-container traffic to localhost goes direct instead of through mitmproxy. A
profile can override this by forwarding the host's own `no_proxy` via
`passthrough`.

Shape:

```yaml
url_rewrites:
  auth:                                      # optional
    username: admin
    token_var: GITEA_TOKEN
  allow_uv_github_fast_path: false           # default false
  default_match_mode: boundary | prefix      # default prefix
  rules:
    - match: <host>/<path-prefix>
      target: <url>
      match_mode: boundary | prefix          # optional; inherits default
```

### Match modes

- `boundary` — **recommended for repository rewrites.** Prefix must
  terminate at a URL path boundary (`/`, `.`, `?`, `#`, or end-of-path),
  scoping the rule to a single repository. `github.com/microsoft/amplifier`
  matches that repo and its git protocol paths but does **not** match
  `microsoft/amplifier-foundation`, `microsoft/amplifier-module-foo`, etc.
- `prefix` — pure path-prefix match (`str.startswith`). Matches every URL
  whose path starts with the prefix, including sibling repositories whose
  names share the prefix. Useful only when you genuinely want to capture a
  whole subtree (e.g. a path prefix that is not a repo name).

The default is `prefix` for backward compatibility. For new profiles,
set `default_match_mode: boundary` on the block — it is almost always
what you want when rewriting repository URLs. Per-rule `match_mode`
overrides the block default. Invalid values raise `ValueError` at load.

### Match order

For each request, rules are evaluated in this order:

1. Host equality — rule's host must equal the request's host exactly.
2. Longest path-prefix first — within matching hosts, rules are sorted by
   descending prefix length. Equal-length prefixes preserve declared order.
3. Path match per rule's `match_mode` — `prefix` accepts any
   `startswith`; `boundary` additionally requires a boundary char after the
   prefix.
4. First match wins — no further rules are evaluated.

The host-side validator (`profile.match_url`) and the in-container proxy
share one matcher (`engine._generate_addon_script` injects the source of
`profile._path_matches` via `inspect.getsource`); they cannot drift.

### Diagnostic warnings

The loader emits these warnings to nudge you toward `boundary` when a rule
looks risky. Both are `UserWarning` subclasses; the loader does not raise.

- `SuspiciousPrefixRuleWarning` — a single `prefix`-mode rule with the
  `/org/repo` shape (two non-empty segments, no trailing boundary char)
  that will silently capture sibling repos. Set `match_mode: boundary` on
  the rule, or `default_match_mode: boundary` on the block.
- `OverlappingRewriteRulesWarning` — two `prefix`-mode rules on the same
  host where one prefix is a prefix of the other. Set `match_mode: boundary`
  on either rule to disambiguate.

### `auth` and credential safety

When `auth` is set, the proxy attaches `Authorization: Basic <token>` to
every matched request. A `prefix`-mode rule that over-matches a sibling
URL sends your credential to whatever target the over-match selected. Use
`match_mode: boundary` (or `default_match_mode: boundary`) on any block
with `auth`. Pinned in `tests/unit/profile/test_url_rewrite_auth_safety.py`.

Non-matching traffic passes through unchanged. Use `url_rewrites` when the
dependency is resolved by URL (e.g. `amplifier-user-sim` redirects
`github.com/microsoft/amplifier-module-provider-anthropic` to Gitea).

### `allow_uv_github_fast_path`

Optional bool, default `false`. When `false`, the DTU exports
`UV_NO_GITHUB_FAST_PATH=true` in the environment so `uv tool install` does a
real `git fetch` through the proxy and the rewrite rules apply correctly.

Why this is the default: uv has a "GitHub fast path" that resolves
`git+https://github.com/<owner>/<repo>@<ref>` by calling `api.github.com`
directly for the commit SHA and fetching `pyproject.toml` from
`raw.githubusercontent.com`. Neither host is covered by URL rewrites, so
with the fast path enabled uv would silently install the **upstream GitHub**
commit even when a Gitea mirror has a different HEAD -- the install
succeeds but at the wrong commit. Disabling the fast path forces uv down
the git fetch path, which the proxy rewrites correctly.

Set `allow_uv_github_fast_path: true` only when you specifically want to
observe or reproduce uv's native behavior (for example, to test what a real
user environment without rewrites would do). In that mode, uv's installs
will bypass `url_rewrites` for any `git+https://github.com/...` URL.

Has no effect when `url_rewrites` is not present (the proxy and its env
vars are only set up when rewrites are configured).

## `pypi_overrides`

Optional. When present, launch resolves wheels on the host, pushes them into
the environment, starts a local `pypiserver`, and exports
`UV_EXTRA_INDEX_URL` / `PIP_EXTRA_INDEX_URL` pointing at that server.

Each package must specify exactly one source:

- `wheel_var`
- `wheel_path`
- `wheel_from_git`

### `wheel_var`

Pass a wheel path through `--var`.

```yaml
pypi_overrides:
  packages:
    - name: my-package
      wheel_var: MY_PACKAGE_WHEEL
```

### `wheel_path`

Point at an existing wheel on disk. Relative paths are resolved relative to the
profile file.

```yaml
pypi_overrides:
  packages:
    - name: my-package
      wheel_path: ./dist/my_package-*.whl
```

### `wheel_from_git`

Clone a repo on the host during launch, build a wheel, and publish it through
the local `pypiserver`.

```yaml
pypi_overrides:
  packages:
    - name: amplifier-core
      wheel_from_git:
        repo: ${GITEA_URL}/admin/amplifier-core.git
        ref: main
        username: admin
        token_var: GITEA_TOKEN
        build_cmd: uv run --with maturin maturin build --release
        wheel_glob: target/wheels/amplifier_core-*.whl
```

Current behavior:

- `ref` defaults to `main`
- if `token_var` is provided, launch injects Basic auth into the clone URL
- the build runs on the host, not inside the environment
- `amplifier-user-sim` uses this for `amplifier-core`

Use `pypi_overrides` when the dependency is resolved by package name rather
than by direct repo URL.

## `passthrough`

Optional.

```yaml
passthrough:
  allow_external: true
  services:
    - name: anthropic
      key_env: ANTHROPIC_API_KEY
```

Current behavior:

- `allow_external` is parsed but is not currently used to enforce network policy
- each `services[].key_env` is copied from the host into the environment if it exists
- `amplifier-user-sim` uses this to forward `ANTHROPIC_API_KEY`

## `access`

Optional. When present, launch sets up Incus proxy devices to forward ports from
the host to the container. This makes services inside the container reachable via
`localhost` on the host machine (including through WSL2 to a Windows browser).

Proxy devices are automatically removed when the container is destroyed.

```yaml
access:
  hostname: amplifier-chat
  ports:
    - host: 8410
      container: 8410
      label: Chat UI
      path: /chat/
```

Fields:

- `hostname` (optional) -- register a `.local` mDNS hostname for this environment
  via Avahi. The `.local` suffix is appended automatically (`amplifier-chat` becomes
  `amplifier-chat.local`). Access URLs will use the hostname instead of `localhost`.
  Can also be set via the `--hostname` CLI flag (which takes priority over the
  profile field). Requires `avahi-daemon` and `avahi-utils` to be installed.
  See [hostname support](#hostname-support) for platform details.
- `host` (required) -- port to listen on the host
- `container` (required) -- port to forward to inside the container
- `label` (optional) -- human-readable name shown in launch output
- `path` (optional, default `/`) -- URL path appended when constructing access URLs
- `verify` (optional, default `true`) -- whether `check-readiness` should
  poll the host-side port after launch
- `verify_timeout` (optional, default `30`) -- seconds to wait for the host
  port to become reachable during verification
- `verify_interval` (optional, default `2`) -- seconds between verification
  polls

When `access.ports` is defined, the launch JSON includes additional fields:

```json
{
  "hostname": "amplifier-chat.local",
  "container_ip": "10.x.x.x",
  "access": [
    {"label": "Chat UI", "url": "http://localhost:8410/chat/", "mdns_url": "http://amplifier-chat.local:8410/chat/"}
  ]
}
```

`url` always points at `localhost`. `mdns_url` is added per entry only when
a `.local` hostname was successfully registered via Avahi.

`amplifier-chat` uses this to expose the web UI on `localhost:8410` (or
`amplifier-chat.local:8410` when hostname registration succeeds).


### Hostname support

When `access.hostname` is set (or `--hostname` is passed), DTU registers a
`.local` hostname via `avahi-publish-address`. This makes access URLs easier to
identify when running multiple DTU instances -- `http://my-project.local:8410`
instead of `http://localhost:8410`.

**Prerequisites:**

```bash
# On Linux or WSL2 (if not already installed - check first)
sudo apt install avahi-daemon avahi-utils
```

**Platform support:**

| Platform | Status |
|----------|--------|
| Native Linux | Fully supported. LAN-wide mDNS resolution. |
| WSL2 | Supported within WSL2. Windows browsers will NOT resolve `.local` names from WSL2. |
| macOS / Windows | Not supported. Warning printed, URLs fall back to `localhost`. |

**Hostname priority order:**

1. `--hostname` CLI flag (highest priority)
2. `access.hostname` in profile YAML
3. Container name (`--name` or auto-generated `dtu-<uuid8>`)

**Lifecycle:** The Avahi process runs as a background subprocess tied to the
container lifecycle. It is automatically killed when the container is destroyed.
If the process is orphaned (e.g. crash), it will die when its parent dies, and
the PID file at `/tmp/dtu-avahi-<container-id>.pid` can be cleaned up manually.

If `avahi-publish-address` is not installed, a warning is printed and access URLs
fall back to `localhost` -- no error is raised.


## `mock_services`

Optional. A list of mock services to run as Docker sidecar containers alongside
the Incus environment. Each service is resolved from a source (local directory or
git URL), built into a Docker image, and started with an ephemeral port mapping.
Mock services are meant to exercise the provisioned code with as close to real-world conditions as possible.

Traffic from inside the Incus container to service domains is routed through
mitmproxy automatically -- code running inside the environment can address mock
services by their real-world hostnames.

```yaml
mock_services:
  - source: /path/to/my-mock-service
    config:
      api_key: my-secret
```

Fields:

- `source` (required) -- local directory path or git URL to the mock service
  repository. Must contain a `digital-twin-mock.yaml` manifest at its root.
- `config` (optional) -- key-value map passed as environment variables to the
  Docker container (keys are uppercased).

### Mock service manifest

Each mock service must have a `digital-twin-mock.yaml` at its root:

```yaml
name: my-service
version: 0.1.0
description: Mock for an external API

runtime:
  type: docker
  build: Dockerfile    # optional, defaults to "Dockerfile"
  port: 3000           # container port the service listens on

domains:
  - api.example.com
  - example.com
```

Fields:

- `name` (required) -- service identifier
- `version` (optional, default `0.0.0`)
- `description` (optional)
- `runtime.type` -- currently only `docker` is supported
- `runtime.build` (optional) -- Dockerfile path relative to the service root
- `runtime.image` (optional) -- pre-built image to use instead of building
- `runtime.port` (required) -- container port the service listens on
- `domains` (optional) -- hostnames that mitmproxy will intercept and route to
  this service. Enables DNS rewriting so code inside the environment can use
  real-world hostnames to reach the mock.

### Lifecycle

Mock service containers are:

- built and started during `launch`, before the mitmproxy proxy is configured
- tracked with Docker labels (`dtu.env-id`, `dtu.mock-name`)
- stopped and removed during `destroy`
- cleaned up on launch failure (best-effort)


## `provision`

Optional. Supports file seeding and shell commands during launch.

```yaml
provision:
  files:
    # Directory push: `seed-data/` lands inside `dest` as `/root/app/seed-data/`.
    # If you want the contents at /root/app/data/, point dest at the parent.
    - src: ./seed-data/
      dest: /root/app/
      recursive: true
    - src: ./config/settings.yaml
      dest: /root/.config/app/settings.yaml
      mode: "0644"
  setup_cmds:
    - apt-get update && apt-get install -y git curl
    - curl -LsSf https://astral.sh/uv/install.sh | sh
```

### `provision.files`

Push host files into the container before `setup_cmds` run.

Fields per entry:

- `src` (required) -- host path. Relative paths are resolved relative to the
  profile file's directory.
- `dest` (required) -- absolute path inside the container.

  Behavior depends on whether `src` is a file or a directory:

  - **File:** `dest` is treated as a file path; the file lands at `dest`.
  - **Directory (with `recursive: true`):** `dest` is treated as the **parent
    directory**, and the source basename is preserved inside it. Pushing
    `./data/` to `/root/app/data/` lands files at `/root/app/data/data/...`;
    pushing the same source to `/root/app/` lands them at `/root/app/data/...`.

- `recursive` (optional, default `false`) -- recursively transfer a directory.
  Required when `src` is a directory; off by default to match the file-only
  case (where leaving it on would create a directory at the destination path
  and put the source inside it).
- `create_dirs` (optional, default `true`) -- create intermediate directories
  in the container if they don't exist.
- `mode` (optional) -- file permission string (e.g. `"0644"`).
- `uid` (optional) -- set file UID on push.
- `gid` (optional) -- set file GID on push.

Files are pushed after environment variables and proxy are configured, so any
`${VAR}` references in `src`/`dest` will already be substituted.

### `provision.setup_cmds`

Shell commands run in order after files are pushed.

Current behavior:

- commands run in order with `bash -lc`
- proxy-related environment variables are already in place before these commands run
- passthrough secrets are already exported before these commands run
- `provision.files` entries are already in place before these commands run
- launch fails on the first non-zero exit code

This is where the current built-in profiles install tools, write config files,
and create working directories.


## `update`

Optional. Defines commands to re-run when updating provisioned software in a
running environment without destroying it. Used by the `update` CLI command.

```yaml
update:
  refresh_pypi: true
  cmds:
    - uv tool install --reinstall --force amplifier
```

`refresh_pypi` (optional, default `false`)
  When `true`, the `update` command re-runs the `pypi_overrides` pipeline before
  executing the update commands. This rebuilds wheels from the current state of
  the source repos (e.g. Gitea), pushes them into the container, and restarts
  pypiserver. The `pypi_overrides` section must be defined in the same profile.

`cmds` (required)
  List of shell commands to run in order with `bash -lc`. Same execution model
  as `provision.setup_cmds` -- proxy variables, PATH, and passthrough secrets are
  available.

The typical workflow:

```bash
amplifier-digital-twin launch amplifier-user-sim --var ...    # full setup
# make code changes, push to Gitea
amplifier-digital-twin update dtu-a1b2c3d4 --var ...          # pull + reinstall
# test in the DTU
# iterate: change code -> update -> test
amplifier-digital-twin destroy dtu-a1b2c3d4                   # done
```

`update` reads the profile from a snapshot stored in the container, so pass the same `--var` values used at launch for any `${VAR}` references in the `update` or `pypi_overrides` sections.


## `readiness`

Optional. A list of checks that define when services inside the environment are
ready to accept connections. When present, the engine stores the checks as
container metadata so the `check-readiness` command can evaluate them.

Readiness checks are evaluated on demand (not during launch). Use
`amplifier-digital-twin check-readiness <id>` to poll.

Each check has a `name` and exactly one of `http`, `tcp`, or `command`:

```yaml
readiness:
  - name: amplifierd-ready
    http:
      url: http://localhost:8410/ready
      expect_json: { "ready": true }

  - name: db-port
    tcp:
      port: 5432

  - name: custom-setup
    command: "test -f /tmp/provisioning-done"
```

### `http`

Runs `curl -sf <url>` inside the container. Passes on HTTP 200.

`expect_json` (optional) does a key-value subset match on the response body:
every key in `expect_json` must be present in the response with the same value,
but the response may contain additional keys.

### `tcp`

Opens a TCP connection to `localhost:<port>` inside the container. Passes when
the port accepts connections.

### `command`

Runs the command via `incus exec`. Passes when exit code is 0.

### Polling

`check-readiness` is stateless -- each invocation runs all checks once and
returns the result. The caller owns the polling loop:

```bash
while ! amplifier-digital-twin check-readiness dtu-a1b2c3d4 \
    | jq -e '.ready'; do
  sleep 3
done
```


## Sample profiles

Profiles in this repo are organized into four buckets by intent:

### `profiles/amplifier/`

Profiles that run an Amplifier experience — the Amplifier CLI, a chat
UI, a dev-machine configuration, or any other first-class Amplifier
feature. Examples:

- [`amplifier-chat`](../profiles/amplifier/amplifier-chat.yaml) — browser-accessible chat UI backed by amplifierd
- [`amplifier-standalone`](../profiles/amplifier/amplifier-standalone.yaml) — standalone Amplifier user environment with the foundation bundle composed, ready for interactive `amplifier` sessions via `exec`

### `profiles/patterns/`

Small, focused profiles that demonstrate a single capability of
`amplifier-digital-twin` — private-repo access, local wheel overrides, URL
rewriting, and so on. Each profile here is meant to be read as a reference.
Example: [`private-github-repo`](../profiles/patterns/private-github-repo.yaml).

### `profiles/tests/`

Profiles whose primary role is validating `amplifier-digital-twin` itself.
The test suite launches these to exercise networking, provisioning,
packaging, and Amplifier user-simulation paths. Treat this bucket as
internal infrastructure. Example: [`docker-in-incus`](../profiles/tests/docker-in-incus.yaml).

### `profiles/community/`

Catch-all bucket for profiles that don't fit the three above - third-party
tool tryouts, experimental setups, and contributor-authored samples.
The profile must launch successfully, but maintainer review is lighter. 
Example: [`openai-codex-cli`](../profiles/community/openai-codex-cli.yaml).


## Self-contained profiles

A profile must launch cleanly on any machine with the documented
prerequisites. Anyone cloning the repo should be able to run
`amplifier-digital-twin launch <profile>` and get the same result as the author.

**Rule:** every file the profile depends on must be fetched by a command
in the profile itself — `curl`, `wget`, or `git clone` inside a
`provision.setup_cmds` step. Do not rely on files that only happen to
exist on the author's host.

Some profiles cannot be fully self-contained — e.g. they reference a
local wheel, require a host-side Gitea mirror, or need a secret managed
outside the repo. When that is unavoidable, the profile must document
the gap in a leading comment block at the top of the YAML:

- what out-of-band state it requires (files, launch `--var`s, env vars,
  host-side services)
- the exact launch invocation the caller must use, including every
  `--var` flag


## Contributing a profile

1. **Fork the repo and clone your fork**.

2. **Pick a bucket and place the file.** Unless you have a strong reason to choose another, use `community/`. The file lives at `profiles/<bucket>/<name>.yaml` — e.g. `profiles/community/my-profile.yaml`.

3. **Confirm the filename stem is unique.** Built-in names resolve recursively across all buckets, so no two profiles may share a stem. Check with:

   ```bash
   find profiles -name '<name>.yaml'
   ```

   Pick a different name if more than one file is returned.

4. **Fill in required metadata.** Every profile must include:
   - `name` (or a filename whose stem matches the intended invocation name)
   - `description` — one sentence the CLI can show
   - A leading comment block describing what the profile does, required
     environment variables, and the `amplifier-digital-twin launch` command
     that exercises it.

5. **Make it self-contained.** See [Self-contained profiles](#self-contained-profiles) above — fetch every dependency inside the profile, or document the exception in the leading comment block.

6. **Verify locally.** Launch the profile end-to-end before submitting:

   ```bash
   amplifier-digital-twin launch <your-profile>
   amplifier-digital-twin check-readiness <id>   # must return ready: true
   amplifier-digital-twin destroy <id>
   ```

7. **Open a PR.** Keep the description brief and factual — what the profile does, why it is useful, what is unique about it.
