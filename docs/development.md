# Development

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (package manager and runner)
- [Incus](https://linuxcontainers.org/incus/) (container runtime)
- [Docker Engine](https://docs.docker.com/engine/install/) (for Gitea instances)
- [amplifier-gitea](https://github.com/microsoft/amplifier-bundle-gitea) on PATH

```bash
# Install amplifier-gitea if you don't have it
uv tool install git+https://github.com/microsoft/amplifier-bundle-gitea@main
```

## Setup

```bash
git clone https://github.com/microsoft/amplifier-bundle-digital-twin-universe.git
cd amplifier-bundle-digital-twin-universe
uv sync
```

## Running the CLI locally

```bash
uv run amplifier-digital-twin --help
uv run amplifier-digital-twin launch amplifier-user-sim
```


## Manual Verification

For a full `amplifier-user-sim` walkthrough that mirrors the same repo changes
used by the end-to-end test, see [manual_verification.md](manual_verification.md).


## Tests

> **Warning:** Integration tests (`--run-integration`, `--run-e2e`) destroy
> **all** `amplifier-digital-twin` managed containers when the test session
> ends, not just the ones the tests created. Any environment you created
> with `amplifier-digital-twin launch` will be removed. Run
> `amplifier-digital-twin list` beforehand and `amplifier-digital-twin destroy`
> anything you want cleanly shut down, or be aware that running environments
> will be force-removed.

Tests invoke `amplifier-digital-twin` as a subprocess via `uv run`, exactly as a
user would on their machine. No in-process test runners or mocks.

```bash
# CLI surface tests (no Incus required)
uv run pytest

# Lifecycle smoke tests (requires Incus running)
uv run pytest tests/test_lifecycle.py --run-integration -v
```

### E2E Tests

The end-to-end coverage is split so you can run the fastest useful suite first.

#### Fast PyPI override test

Exercises `wheel_from_git` with a tiny temporary package repo.

**Prerequisites:**
- Incus running

```bash
uv run pytest tests/test_e2e_pypi.py --run-e2e -v -s
```

#### Fast single-module rewrite test

Launches `amplifier-user-sim-single-module`, mirrors
`amplifier-module-provider-anthropic` into Gitea, pushes a marker change on
top, and verifies the rewritten provider is loaded.

**Prerequisites:**
- Incus running
- Docker running
- `amplifier-gitea` installed on PATH
- GitHub token (`GH_TOKEN`, `GITHUB_TOKEN`, or `gh auth login`)

```bash
uv run pytest tests/test_e2e_amplifier_user_sim_single_module.py --run-e2e -v -s
```

#### Full amplifier-user-sim end-to-end test

Mirrors both `amplifier-core` and
`amplifier-module-provider-anthropic` into Gitea, pushes local changes on top,
launches `amplifier-user-sim`, and verifies:

- the installed Amplifier tool environment uses the overridden `amplifier-core`
  wheel
- running Amplifier loads the rewritten `amplifier-module-provider-anthropic`
  source and completes a real Anthropic-backed run

**Prerequisites:**
- Incus running
- Docker running
- `amplifier-gitea` installed on PATH
- GitHub token (`GH_TOKEN`, `GITHUB_TOKEN`, or `gh auth login`)
- `ANTHROPIC_API_KEY`

```bash
uv run pytest tests/test_e2e_amplifier_user_sim.py --run-e2e -v -s
```

To run all end-to-end suites:

```bash
uv run pytest tests/test_e2e_pypi.py \
  tests/test_e2e_amplifier_user_sim_single_module.py \
  tests/test_e2e_amplifier_user_sim.py \
  --run-e2e -v -s
```
