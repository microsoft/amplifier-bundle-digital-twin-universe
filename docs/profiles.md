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
```

Right now the engine only consumes `base.image`.

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
