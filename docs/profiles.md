# Profile Reference

A profile can be launched by:

- built-in name, for example `amplifier-user-sim`
- relative path to a YAML file
- absolute path to a YAML file

Examples can be found in the [profiles/](../profiles/) directory.


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
  config:                              # optional
    security.nesting: "true"
```

`base.image` (required) selects the container image.

`base.config` (optional) passes Incus container config flags at creation time.
Each key-value pair becomes an `incus launch --config key=value` argument. Use
this for Incus-level settings like `security.nesting` (required for running
Docker inside the environment), `limits.cpu`, `limits.memory`, etc.

See [docs/docker-in-incus.md](docker-in-incus.md) for the Docker nesting use
case.

## `url_rewrites`

Optional. When present and fully resolved, launch configures a mitmproxy-based
HTTPS proxy inside the environment and exports `HTTP_PROXY` / `HTTPS_PROXY`
for later provisioning commands and interactive use.

Rules are matched at exact repo granularity:

- `github.com/microsoft/amplifier` matches that repo only
- it does not match `github.com/microsoft/amplifier-core`

Current shape:

```yaml
url_rewrites:
  auth:
    username: admin
    token_var: GITEA_TOKEN
  rules:
    - match: github.com/microsoft/amplifier-module-provider-anthropic
      target: ${GITEA_URL}/admin/amplifier-module-provider-anthropic
```

Current behavior:

- `auth` is optional
- if present, Basic auth credentials are injected into rewritten requests
- all non-matching traffic passes through unchanged
- this is what `amplifier-user-sim` uses to redirect
  `github.com/microsoft/amplifier-module-provider-anthropic` to Gitea

Use `url_rewrites` when the dependency is resolved by URL.

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
  ports:
    - host: 8410
      container: 8410
      label: Chat UI
      path: /chat/
```

Fields:

- `host` (required) -- port to listen on the host
- `container` (required) -- port to forward to inside the container
- `label` (optional) -- human-readable name shown in launch output
- `path` (optional, default `/`) -- URL path appended when constructing access URLs

When `access.ports` is defined, the launch JSON includes additional fields:

```json
{
  "container_ip": "10.x.x.x",
  "access": [
    {"label": "Chat UI", "url": "http://localhost:8410/chat/"}
  ]
}
```

`amplifier-chat` uses this to expose the web UI on `localhost:8410`.


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

Optional. Right now only `provision.setup_cmds` is implemented.

```yaml
provision:
  setup_cmds:
    - apt-get update && apt-get install -y git curl
    - curl -LsSf https://astral.sh/uv/install.sh | sh
```

Current behavior:

- commands run in order with `bash -lc`
- proxy-related environment variables are already in place before these commands run
- passthrough secrets are already exported before these commands run
- launch fails on the first non-zero exit code

This is where the current built-in profiles install tools, write config files,
and create working directories.


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
