# DONE-NOTE — lane `j1e6-ci-dtu`

**Item:** `model_performance-j1e6` — *"CI for the 19 repos that have NO `.github/workflows` at all — red-then-green proven, one PR per repo"*
**Repo slice:** `microsoft/amplifier-bundle-digital-twin-universe`
**Date:** 2026-09-07
**Outcome:** **branch A — every deliverable DONE**, shipped for landing as PR #35 (ready for review, all five checks green, **not merged** — the merge is the manager's stage).
**Spend:** **$0.00** against a **$0** authority. CI minutes only: 3 gating runs × 5 checks. No API calls, no DTU, no containers, nothing registered in the infra ledger, nothing to tear down.

---

## Deliverables

| deliverable | state |
|---|---|
| `.github/workflows/ci.yml` running the real suite, ruff pinned, `push:main` + `pull_request`, no path filters / `continue-on-error` / `\|\| true` | **DONE** |
| BOTH run URLs quoted in the PR body; RED job log shows the suite executing with a genuine **test** failure | **DONE** |
| Scratch PR closed and its branch deleted — verified, not assumed | **DONE** |
| A statement of what the suite actually covers | **DONE** — 303 real tests; 77 deliberately excluded, labelled |
| Clean main red → STOP, report, fix as separate named commits | **DONE** — two genuine findings, two named commits |
| DRAFT PR, marked ready when green, **not merged** | **DONE** — PR #35 |

---

## What shipped

**PR:** https://github.com/microsoft/amplifier-bundle-digital-twin-universe/pull/35
**Branch:** `lane/j1e6-ci-dtu`

Three job definitions → **five checks** on `push:main` + `pull_request:main`:

| check | command |
|---|---|
| Lint | `uvx ruff@0.16.6 check --isolated --select E4,E7,E9,F .` |
| Tests (Python 3.11 / 3.12 / 3.13) | `uv run --python <ver> pytest tests/unit/ -q --tb=short` |
| Bundle structure (YAML) | inline Python: parse `bundle.md` frontmatter + `behaviors/*.yaml`, require `bundle.name` |

Commits, in order:

| sha | commit |
|---|---|
| `d6c5999` | `fix(tests): drop unused json import flagged by ruff F401` |
| `307855a` | `fix(tests): mock incus.list_instances so the unit suite really needs no Incus` |
| `0fd94f8` | `ci: add .github/workflows/ci.yml — lint, 303-test suite, bundle structure` |

---

## The red-then-green gate

| run | tree | result |
|---|---|---|
| [34156161999](https://github.com/microsoft/amplifier-bundle-digital-twin-universe/actions/runs/34156161999) | first red proof, before the incus fix | **RED** — `8 failed, 296 passed`. Surfaced finding 1 (below). |
| [34156379045](https://github.com/microsoft/amplifier-bundle-digital-twin-universe/actions/runs/34156379045) | red proof re-run after the fix | **RED** — `1 failed, 303 passed` on all three Pythons. **This is the quoted RED run.** |
| [34156530012](https://github.com/microsoft/amplifier-bundle-digital-twin-universe/actions/runs/34156530012) | `0fd94f8`, workflow-only tree | **GREEN** — `303 passed` ×3, `All checks passed!`, `Bundle structure OK.` |

Three deliberate defects on the scratch branch, one per job — so a single run proved all three job types can go red **for their own reason**:

```
Tests (3.11/3.12/3.13)  1 failed, 303 passed
                        FAILED tests/unit/test_ci_red_proof.py::test_ci_can_go_red
                          - AssertionError: deliberate failure
Lint                    F401 [*] `os` imported but unused
Bundle structure        - behaviors/_red_proof.yaml: mapping values are not allowed here
```

The test failure is a genuine assertion inside a suite that **collected and executed the repo's real 303 tests** — not a setup error, not a lint error, which would have proved nothing.

**Scratch PR #34 CLOSED, branch `ci/red-proof-j1e6` DELETED — verified by remote read**, `git ls-remote --heads origin ci/red-proof-j1e6` → 0 lines. Not inferred from the delete command's own success message.

Full job logs: `evidence/RED-run-34156161999.log`, `evidence/RED-run-34156379045.log`, `evidence/GREEN-run-34156530012.log`.

---

## FINDING 1 — seven "unit" tests silently required a real Incus binary

**This is the headline result of the lane, and it is exactly what a CI catches and a local run structurally cannot.**

The first red-proof run reported `8 failed, 296 passed` where **one** failure had been planted. The other seven:

```
tests/unit/test_default_base_config.py::test_launch_*   (4)  FileNotFoundError: [Errno 2] 'incus'
tests/unit/test_nested_incus.py::test_launch_*          (3)  FileNotFoundError: [Errno 2] 'incus'
```

Both modules assert in their own docstrings: *"No Incus, Docker, or any real container runtime is required. All subprocess calls are mocked."* **That contract has been false since `38f3ff4`** (the `launch --max-instances` guard, PR #31), which added a new call to `launch()`'s path:

```
engine.launch()
  -> _enforce_max_instances()        engine.py:1131
    -> count_live_instances()        engine.py:1101
      -> incus.list_instances()      engine.py:1089
        -> subprocess.run(["incus", ...])   incus.py:742
```

Neither fixture mocks `list_instances`, because it did not exist when they were written. **Every developer machine in this program has Incus installed**, so the call quietly succeeded and nothing ever looked wrong. On a clean machine all seven die.

**Fix (`307855a`):** mock at the `incus` boundary — `monkeypatch.setattr(incus_mod, "list_instances", MagicMock(return_value=[]))` — in both `_patch_launch_infra` fixtures. Mocked there rather than at `count_live_instances` so the guard's own code path is still exercised. No assertion changed.

**Fail-before / pass-after, measured locally with `incus` removed from `PATH`** (a shim `PATH` mirroring `/usr/bin`, `/bin`, `/usr/local/bin`, `/snap/bin` minus `incus*`):

```
before   tests/unit/test_nested_incus.py + test_default_base_config.py  ->  7 failed, 7 passed
after    tests/unit/                                                     ->  303 passed
```

Confirmed in CI: the same suite went `296 passed` → `303 passed` across the fix.

**Transferable to the sibling CI lanes:** a test file whose docstring claims "no real runtime required" is a claim, not a guarantee. On a host where the runtime happens to be installed, a leaked `subprocess.run` is indistinguishable from a mock. The only instrument that tells them apart is a machine without the runtime — i.e. CI, or a stripped `PATH`.

---

## FINDING 2 — one genuine ruff `F401` on clean main

`tests/e2e/features/test_docker_in_incus.py:22` imports `json` and never uses it (the module reaches JSON through the `run_cli_json` helper — a *different name*, which is why it reads as used at a glance). It was the **only** finding in the pinned rule set repo-wide at `5b3767d`.

Fixed as its own named commit (`d6c5999`), never by narrowing the lint selection or adding `continue-on-error`. Behaviour-neutral: the module still collects its 4 e2e tests; `pytest tests/unit/` stays at 303 passed.

---

## What the suite actually covers

**303 tests — a real suite, not an import smoke.** CLI argument parsing, profile resolution and validation, `url_rewrite` matching / auth-safety / target validation, addon matcher parity, file push **and** pull directory recursion, incus launch-timeout handling, the max-instances guard, nested-incus and proxy-env handling, hostname and visual-id, and the lean-head guardrail.

**77 tests are deliberately NOT run in CI** — everything under `tests/integration/` and `tests/e2e/`. The repo's own `conftest.py` deselects them without `--run-integration` / `--run-e2e`, and they need a live Incus daemon, Docker, and API keys. Stated in the PR body rather than left to look like coverage.

Note the standing `tests/unit/` command was **already** covering `tests/unit/test_lean_head_guardrail.py` and its vendored v1 fixture, which the item description flagged as "nothing currently runs". As of this PR, CI runs it on every push and PR.

---

## Deliberate scope-outs, reported not omitted

- **Ruff 0.16's FULL modern default tier is not used and would be red: 101 findings**, none of them breakage —
  32 `PLW1510` (`subprocess.run` without `check=`), 31 `I001`, 16 `BLE001`, 6 `SIM117`, 5 `ISC004`, 4 `S110`,
  3 `TRY004`, 1 each `SIM114` / `S102` / `PERF102`, plus the `F401` fixed above.
  Split 52 in `src/`, 49 in `tests/`. Adopting any of it is a source change, out of scope for a workflow PR.
  The pinned `E4,E7,E9,F` set is ruff's own classic default tier: pyflakes plus pycodestyle **errors**.
- **`ruff format --check` is not run**, same reason.
- **`enable-cache:` is deliberately absent from `setup-uv`.** It keys on `**/uv.lock`; this repo commits no
  lockfile (`.gitignore` line `uv.lock`), so enabling it hard-fails setup before ruff or pytest runs — a red
  that proves nothing. Inherited from the sibling wayfinder lane's own first red-proof run. `enable-caching:`
  (with an `-ing`, as in the browser-tester lane's file) is **not a valid input** and is silently ignored;
  "correcting" it here would have hard-failed setup.
- **`uv sync --frozen` is not used** (the context-intelligence template's shape) — it fails outright without
  a committed lockfile.

---

## Incidents and self-corrections

1. **`gh pr edit --body-file` reported nothing useful and did not apply the body.** It emitted a GraphQL
   *"Projects (classic) is being deprecated"* error and left the PR body carrying the literal placeholder
   `GREEN_RUN_URL`. Caught by reading the body back with `gh pr view --json body`; re-applied with
   `gh api -X PATCH .../pulls/35 -F body=@file` and verified by a second read-back. **A PR body is only as
   good as the read-back** — the same rule the publication marker enforces for branches.
2. **The first red-proof run was kept, not discarded.** It is the evidence for finding 1. The gate was then
   re-proved on the fixed tree so the quoted RED run shows the clean `1 failed, 303 passed` shape.
3. **A sibling lane on this host clobbered `/tmp/pr_body.md`, and one `PATCH` briefly published THEIR PR
   body onto PR #35.** Parallel lanes share `/tmp`. This lane staged its PR body at the obvious shared path;
   a sibling CI lane (a bundle with `modules/tool-skills` and a new `ruff.toml` — not this repo) wrote its
   own body to the same filename between my write and my next read. The next `gh api -X PATCH
   ... -F body=@/tmp/pr_body.md` published that text as mine. Caught within one command by reading the body
   back (`gh pr view 35 --json body`) instead of trusting the `PATCH`'s 200. Corrected by re-writing the
   body to a **lane-private** path (`lanes/j1e6-ci-dtu/.lane-tmp/`) and re-`PATCH`ing, then verifying:
   0 occurrences of the sibling's text, both run URLs present.
   **Checked the blast radius in the other direction, read-only:** the two other live `lane/j1e6-ci-*` PRs
   (`amplifier-bundle-skills#68`, `amplifier-bundle-modes#32`) carry **no** text from this repo — modes'
   single "DTU" hit is its own spend line ("no API calls, no DTU"). Nothing of mine leaked outward. No
   other repo was edited.
   **Rule for every parallel-lane batch: never stage anything at a fixed `/tmp/<generic-name>`.** A shared
   path plus N concurrent lanes is a silent cross-write, and `PATCH` returning 200 proves only that
   *something* was published, never that it was yours.

---

## Goal / process defects worth fixing before the next multi-lane item

**This item is one item with nineteen lanes** (its own description says so), but the per-lane goal template
applies a **single-lane claim/resolve procedure** to it:

- Procedure 1 says a refused claim ⇒ write `BLOCKED.md` and stop. On a one-item/many-lanes item at most one
  lane can hold it, so **every other lane is instructed to declare itself blocked over the designed steady
  state.** Had all 19 CI lanes obeyed literally, the owner directive would have produced 19 `BLOCKED.md`
  files and no CI.
- Procedure 5 ends in `work_resolve`. The item was **already resolved** (2026-09-07T18:14:01Z, covering
  wayfinder only), so `work_resolve` from this lane would fail on differing text, and `work_reopen` would
  clear `closed_at` and move every throughput roll-up.

**What this lane did instead** — the same route the browser-tester sibling took, and for the same reason:
read the authoritative spec with `work_list(item_id=...)` (full description + acceptance criteria, **no claim,
no mutation, no custody**), completed every deliverable, and recorded completion with `work_erratum`
(append-only, no claim required, never rewrites the stored resolution).

**The fix for the next one:** either file one item per repo, or have the goal say *"claim if free; if a sibling
holds it, proceed and record per-repo completion via `work_erratum`, and let the holder or the manager resolve
once every lane has landed."*

---

## Spend ledger

| item | amount |
|---|---|
| API calls | $0.00 |
| DTU launches | $0.00 |
| Containers | $0.00 |
| **Total** | **$0.00** of a **$0.00** authority |

The `$0` authority closes trivially here: the deliverable buys no runs, so the arithmetic is
`0 runs × 0 arms × $0 = $0.00`, slack $0.00, and every deliverable landed inside it. No residue, nothing
unspendable, no cap-bound NOT-POSSIBLE. Nothing was registered in the infra ledger; there is nothing to
tear down.

---

## For the manager

1. Merge PR #35 (squash or merge, either is fine — the three commits are individually meaningful).
2. Then confirm main HEAD reports a successful check-run — **configured is not installed**:
   ```
   gh api repos/microsoft/amplifier-bundle-digital-twin-universe/commits/main/check-runs \
     --jq '.check_runs[] | "\(.name): \(.conclusion)"'
   ```
   Expect five: `Lint`, `Tests (Python 3.11)`, `Tests (Python 3.12)`, `Tests (Python 3.13)`,
   `Bundle structure (YAML)`.
3. Finding 1 is a bug that existed on main before this PR and is fixed by it. If any other repo in this
   program vendors or copies these fixtures, it carries the same latent break.
