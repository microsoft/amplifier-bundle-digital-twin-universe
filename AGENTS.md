# AGENTS.md — amplifier-bundle-digital-twin-universe

Conventions for AI coding agents (Amplifier, Claude Code, Cursor, etc.) and human contributors using them. Read this before making changes.

## Key files

@README.md
@docs/development.md
@docs/api-reference.md
@docs/profiles.md
@docs/amplifier-user-sim-launch-flow.dot

### Agents

@agents/dtu-profile-builder.md

### Example profiles
Please update these profiles anytime we make relevant changes.

@profiles/tests/amplifier-user-sim.yaml
@profiles/amplifier/amplifier-chat.yaml

## What this repo is

CLI and library for spawning and managing **Digital Twin Universe (DTU)** environments. Wraps the `incus` CLI with profile-driven provisioning, file push/pull (recursive), exec, and lifecycle management. The DTU is how the broader ecosystem gets an isolated, realistic environment for integration verification.

## Key directories

- `src/amplifier_bundle_digital_twin_universe/` — CLI entry points and library modules. `incus.py` and `engine.py` are the operational hot paths.
- `tests/unit/` — pytest unit tests covering CLI, library, and provisioning logic.
- `tests/integration/` — real-DTU integration tests (if present in the current tree).
- `scripts/` — test and helper scripts (smoke runners, ad-hoc verification harnesses).

## Test commands

Run these before opening a PR. The reviewer expects evidence in the PR body, not just "tests pass."

- **Unit tests**: `pytest tests/unit/`
- **Live DTU test** (required when touching provisioning, `incus.py`, `engine.py`, file-push, file-pull, or anything that shells out to `incus`): actually launch a throwaway DTU and exercise the changed operation. Capture `incus launch` output and the result of the operation. Delete the DTU after the test (`incus delete --force <name>`) to confirm idempotency.

## Verification gradient

| Change type | Required verification |
|---|---|
| `incus.py`, `engine.py`, anything calling `subprocess.run` against `incus` | Unit tests **and** a live DTU test exercising the changed operation. Paste output in the PR. |
| Provisioning logic, profile rendering | Unit tests **and** a live DTU launch from a representative profile. |
| File push/pull code paths | Unit tests **and** a live test that pushes/pulls a directory (not just a file) — the recursive path is the one that historically broke. |
| CLI argument parsing, help text | Unit tests sufficient. |
| Doc changes | Unit tests sufficient. |

## Common pitfalls (from session experience)

- **Hardcoded subprocess timeouts**: `subprocess.run(..., timeout=N)` values that look reasonable on a quiet laptop are too short on loaded systems. Prefer environment-variable overrides like `AMPLIFIER_DTU_INCUS_LAUNCH_TIMEOUT_SECONDS` where they exist; add one where they don't, rather than picking a new fixed number.
- **Directory recursion in file-push / file-pull**: both code paths now handle directories recursively. Older code assumed file-only and silently no-op'd on directories. If you find a "file not transferred" bug, check whether the caller is passing a directory through a path that still assumes a single file.
- **`mirror-from-github` fragility on ARM64**: the `amplifier-bundle-gitea` mirror-from-github path has known ARM64 reliability issues. When reliability matters (e.g., in test setup), prefer direct Gitea API calls over mirror-from-github.
- **Incus is not Docker**: `incus exec`, `incus file push`, and `incus file pull` have different ergonomics from their Docker equivalents. Bind-mount semantics, networking defaults, and user-namespace behavior all differ. Don't transliterate Docker patterns; check the `incus` man pages.

## When in doubt

- Read `src/amplifier_bundle_digital_twin_universe/incus.py` for the canonical shape of an `incus` shell-out.
- Per-repo conventions documented here take precedence over generic foundation guidance for this repo's internals.

## PR checklist

`.github/PULL_REQUEST_TEMPLATE.md` will appear automatically when you open a PR. Honor it. The boxes are not decorative.
