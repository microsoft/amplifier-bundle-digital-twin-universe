# Tests

The test suite is organized by what each test costs to run, then by what it tests.

```
tests/
├── unit/                     # default — no external deps, milliseconds per test
├── integration/              # @pytest.mark.integration — needs Incus
├── e2e/                      # @pytest.mark.e2e — needs Incus + Docker + (sometimes) API keys
└── point_time_bugfixes/      # historical regressions, not maintained long-term
```

`@pytest.mark.integration` and `@pytest.mark.e2e` tests are skipped by default.
Opt in with `--run-integration` and `--run-e2e` respectively. See
[../docs/development.md](../docs/development.md) for the full command list.


## Layout

```
tests/
├── conftest.py               # CLI options, gating markers, shared fixtures
├── helpers.py                # CLI runners, git/Gitea utilities, find_free_port
│
├── unit/
│   ├── test_hostname.py            # HostnameManager, fully mocked
│   ├── test_visual_id.py           # --visual-id CLI flag, fully mocked
│   ├── test_readiness_access.py    # _poll_port + verify_access_ports against localhost
│   └── profile/
│       ├── test_resolution.py            # find_profile_path() lookup logic
│       ├── test_smoke.py                 # every shipped profile parses cleanly
│       ├── test_validation_warnings.py   # UnknownProfileFieldWarning behaviour
│       └── test_validation_schema_sync.py# schema↔dataclass field-sync contract
│
├── integration/
│   └── test_lifecycle.py     # launch / exec / status / list / destroy
│
├── e2e/
│   ├── features/                         # cross-cutting features
│   │   ├── test_exec_stream.py
│   │   ├── test_file_ops.py
│   │   ├── test_visual_id.py
│   │   ├── test_update_profile_snapshot.py
│   │   ├── test_docker_in_incus.py
│   │   ├── test_access_verification.py
│   │   ├── test_hostname.py
│   │   └── test_pypi.py
│   │
│   └── profiles/                         # full profile-launch verification
│       ├── test_amplifier_chat.py
│       ├── test_amplifier_user_sim.py
│       └── test_amplifier_user_sim_single_module.py
│
└── point_time_bugfixes/
    └── test_e2e_uv_fast_path_bypass.py
```


## Prerequisites by tier

### Unit tests

No prerequisites beyond `uv`. Pure-Python with mocked subprocess/filesystem
calls; `test_readiness_access.py` spins up an in-process `http.server` on
localhost.

```bash
uv run pytest tests/unit/
```

### Integration tests

- Incus running

```bash
uv run pytest tests/integration/ --run-integration -v
```

### E2E features

All e2e tests need:

- Incus running
- `uv` on PATH

Additional per-file requirements:

- `test_docker_in_incus.py`, `test_access_verification.py`, `test_hostname.py`
  need Incus configured with `security.nesting=true` (Docker inside Incus).
- `test_hostname.py` also needs `avahi-daemon` running and `avahi-utils`
  installed on the host (for `avahi-resolve-host-name`).

```bash
uv run pytest tests/e2e/features/ --run-e2e -v -s
```

### E2E profile-launch

These exercise complete profile launches with passthrough APIs and Gitea.

`test_amplifier_chat.py` needs:

- Incus running
- `ANTHROPIC_API_KEY`

`test_amplifier_user_sim_single_module.py` needs:

- Incus running
- Docker running
- `amplifier-gitea` installed on PATH
- GitHub token (`GH_TOKEN`, `GITHUB_TOKEN`, or `gh auth login`)
- A local checkout of `amplifier-module-provider-anthropic` adjacent to this repo

`test_amplifier_user_sim.py` needs everything above plus:

- `ANTHROPIC_API_KEY`
- A local checkout of `amplifier-core` adjacent to this repo

```bash
uv run pytest tests/e2e/profiles/ --run-e2e -v -s
```

### Point-in-time bugfixes

Regression tests for specific bugs that depend on live external state (real
GitHub repos, current `uv` internals, etc.). They are tagged `@pytest.mark.e2e`
but are explicitly **not** maintained long-term — keep them passing while the
bug they cover is still recent, retire them when their assumptions drift.

```bash
uv run pytest tests/point_time_bugfixes/ --run-e2e -v -s
```


## Selecting tests

Run by tier:

```bash
uv run pytest tests/unit/                                # default-skipped tests stay skipped
uv run pytest tests/integration/ --run-integration       # Incus only
uv run pytest tests/e2e/features/ --run-e2e              # Incus + Docker, no API keys
uv run pytest tests/e2e/profiles/ --run-e2e              # everything
```

Run a single file:

```bash
uv run pytest tests/e2e/features/test_file_ops.py --run-e2e -v -s
```

Run by marker (works across the whole tree):

```bash
uv run pytest --run-e2e -m e2e
uv run pytest --run-integration -m integration
```


## Conventions

- Tests invoke `amplifier-digital-twin` as a subprocess via `uv run`, exactly
  as a user would. No in-process test runners or mocks at the e2e layer.
- Every test that creates a DTU instance must call `register_dtu_instance(id)`
  immediately after `launch` so the session-scoped cleanup fixture can
  force-delete it on teardown. Cleanup only touches instances registered by
  the current session — manual environments are left alone.
- Helpers live in `helpers.py` (importable as `from helpers import ...` thanks
  to `pythonpath = ["tests"]` in `pyproject.toml`).
- Fixtures shared across tiers live in `conftest.py` at the tests root.
