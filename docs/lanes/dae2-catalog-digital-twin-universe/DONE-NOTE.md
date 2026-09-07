# dae2 — delegate-catalog SOURCES sweep, amplifier-bundle-digital-twin-universe

**Work item:** `model_performance-01k6` (per-repo child, `relates-to model_performance-dae2`).
The parent was already held by another lane when this one woke
(`claim model_performance-dae2 ... failed: issue already claimed by agent-spark-1-4096607`),
so this lane used the established recovery pattern for this batch — file a per-repo child,
claim and resolve that. Precedent: `model_performance-k75p`.

**Landing stage:** draft PR. This lane may not merge. Fail-before / pass-after is demonstrated
below; the merge is the manager's next stage.

**Spend:** $0 of $0 authority. Text edits, two `validate-agents` recipe runs, static byte counts,
local `pytest` + `ruff`. No API measurement, no eval launches, no DTU launched.

---

## 1. Scope, verified against CURRENT origin/main — not an inherited census number

`origin/main` had moved since this lane's branch point. The branch was rebased onto
**`50c5d37eeb1ca4b438dc8c364d3f6b550aa08bb5`** (`ci: add CI ... (#35)`) and every number below is
measured against that commit, not against the program's original census.

**Delegate-catalog agent set for this repo: exactly one.**
`validate-agents` v1.8.0 discovery, run on the branch, reports it independently:

```
scan_patterns:   ["<repo>/**/agents/*.md", "<repo>/agents/*.md"]
excluded_parts:  [".git", ".venv", "docs", "node_modules", "test-fixtures", "tests"]
candidates_scanned: 1
total_count:        1        location_counts: {"agents/": 1}
non_agent_count:    0
classifier:      frontmatter declares a top-level `meta:` key
```

The GOAL quoted `~1,516 ch` for this agent from the program's earlier census. **Measured on current
origin/main it is 1,468 chars** (the `meta.description` scalar as YAML loads it). The earlier figure
is not reproduced here; ~48 chars of drift, direction unverified. Every figure in this note is the
freshly measured one.

---

## 2. Before / after

| | STOCK (`origin/main` 50c5d37) | LEAN (branch) | Δ |
|---|---:|---:|---:|
| `meta.description` chars | **1,468** | **598** | **−870 (−59.3 %)** |
| `meta.description` bytes | 1,472 | 604 | −868 |
| est. tokens (chars/4, the validator's own estimator) | 367 | 149 | −218 |
| `<example>` blocks | **2** | **0** | −2 |
| `<commentary>` blocks | 0 | 0 | 0 |
| body chars | 28,119 | 28,119 | **0** |

**Repo total: 1,468 → 598 chars, −870 (−59.3 %).** One agent, so the per-agent figure *is* the repo
figure. This field is concatenated into the `delegate` tool description and is therefore paid on
**every request of every session that composes this bundle**, delegated to or not.

Budget: the cap is ≤~600 chars. **598 ≤ 600** — under `validate-agents` v1.8.0's `WARN >600`
threshold, so it fires neither the warning nor the `ERROR >1200` tier.

Reproduce: `python3 docs/lanes/dae2-catalog-digital-twin-universe/evidence/census.py` from the repo
root. Raw output: `evidence/census.json`.

---

## 3. Bodies byte-identical — md5 quoted both sides

Body := everything after the closing `---` of the YAML frontmatter (`raw[raw.index("\n---", 3)+4:]`).

```
STOCK  agents/dtu-profile-builder.md   body  28,119 chars   md5 dd26089828b867a15a3ca91c6f906af4
LEAN   agents/dtu-profile-builder.md   body  28,119 chars   md5 dd26089828b867a15a3ca91c6f906af4
                                                     sha256 42cfd5745135bc9f22398e047b5c5b5d
                                                            97908adf27ea9634a67b855a2ea7f96b  (both)
```

**Identical.** Only the frontmatter `description` value changed. `model_role`
(`[reasoning, coding, general]`) and `provider_preferences` (`anthropic / claude-opus-*`) are
byte-identical too — asserted programmatically in `census.py`
(`non_description_frontmatter_unchanged: true`).

Because the body may not change, the two `<example>` blocks were **deleted, not relocated**. That is
the policy, not a shortcut: `description-authoring-principles.md` V3 rejects `<example>` blocks
outright, on the P2 delegation-wave measurement that stripping every example and commentary tag
produced **no reduction in delegation quality** on either provider tested. §4 shows the two blocks
carried no routing fact the lean description does not state.

---

## 4. FIDELITY TABLE — `dtu-profile-builder`

Every USE WHEN / DO NOT USE WHEN fact, trigger condition and constraint in the stock description,
checked against the lean one.

| # | Fact in STOCK | In LEAN? | Where |
|---|---|---|---|
| 1 | Builds and launches DTU profiles for **any user project** | ✅ | "generates and launches an end-to-end DTU profile … Any project" |
| 2 | Explores the repository | ✅ | "explores the repo" |
| 3 | Generates a **complete** DTU profile | ✅ | "generates … an end-to-end DTU profile" |
| 4 | Launches the environment | ✅ | "launches" |
| 5 | Hands back access details | ✅ | verbatim |
| 6 | For **realistic isolated** testing | ✅ | "must run as if deployed … in an isolated container" |
| 7 | **USE WHEN** a developer wants to test their project in an isolated container | ✅ | promoted to the **first clause** |
| 8 | Project shapes: web app, CLI tool, service with external dependencies | ✅ | verbatim |
| 9 | Any project, any language, any deployment style | ✅ | verbatim |
| 10 | **Distinct from `setup-digital-twin`** (Amplifier-ecosystem-specific DTU setup) | ✅ | restated as an explicit **DO NOT USE** clause |
| 11 | Authoritative on: language and framework detection | ✅ | "detects language/framework" |
| 12 | Authoritative on: containerized dependency analysis | ✅ | "containerized dependencies" |
| 13 | Authoritative on: port forwarding | ✅ | verbatim |
| 14 | Authoritative on: API passthrough | ✅ | "external-API passthrough" |
| 15 | Authoritative on: end-to-end DTU launch for user-built software | ✅ | "end-to-end DTU profile" |
| 16 | `<example>` 1 — scenario: a FastAPI app to Digital-Twin | ✅ | generalised by #8 ("web app") |
| 17 | `<example>` 1 — the `delegate(...)` call shape, incl. `context_depth="recent"`, `context_scope="conversation"` | ✅ | **not a description fact.** Present **verbatim** in `context/dtu-awareness.md` — the same always-on head — and pinned there by `tests/unit/test_lean_head_guardrail.py::REQUIRED_RULES`. Not lost from the head. |
| 18 | `<example>` 2 — scenario: a locally built CLI tool, tested as a real user | ✅ | generalised by #8 ("CLI tool") |
| 19 | `<example>` 2 — "isolated environment with all dependencies … exec access" | ✅ | "isolated container" + "hands back access details" |

**Facts present in STOCK and ABSENT from LEAN: NONE.** Nothing restored; no byte delta owed.

### One phrasing change, disclosed rather than buried

The stock description spells out **"Digital Twin Universe"**; the lean one uses **"DTU"** and the
literal `digital-twin-universe` (in the skill pointer). The expansion is not gone from the head:
`context/dtu-awareness.md` opens `# Digital Twin Universe (DTU)` and is composed into the same head
by this bundle's own behavior. Restating it in the description would be V1's "state each rule once"
violation, paid per turn, for a term the head already carries.

### Three net ADDITIONS (disclosed — additions are not fidelity)

Each is sourced from this repo, not invented:

- **"Gitea for unpublished local code"** — agent body §4 ("Only if the project has local unpublished
  code"). A real routing signal that stock's description omitted.
- **"verifies it"** — agent body §8 (Verify), a required workflow step stock's description omitted.
- **"or DTU questions, install help or troubleshooting — load the digital-twin-universe skill"** —
  verbatim policy from this bundle's own `context/dtu-awareness.md` ("Other DTU needs (general
  questions, install help, troubleshooting): `load_skill(skill_name="digital-twin-universe")`").
  This closes the repo's most likely mis-route: a question that costs a full agent spin-up.

---

## 5. `validate-agents` — fail-before / pass-after

Recipe `foundation:recipes/validate-agents.yaml` **v1.8.0**, foundation pinned `@v2.1.2`
(`a27d5824517d078097b60d84779dd3eae80202cd`), engine `v2-closed-world-legacy-engine`.

| arm | repo_path | verdict | errors | warnings | agents |
|---|---|---|---:|---:|---:|
| **STOCK** | `/tmp/dtu-stock-dae2` (worktree of `origin/main` 50c5d37) | **❌ FAIL** (1 critical) | **2** | 1 | 1 |
| **BRANCH** | this worktree | **⚠️ PASS WITH WARNINGS** | **0** | 1 | 1 |

Verdicts quoted verbatim from the runs; full captures in
`evidence/validate-agents-STOCK-main.txt` (`run-0f1a33de2991`) and
`evidence/validate-agents-BRANCH.txt` (`run-7f5f919ae775`).

**Discovered agent count for this repo: 1**, in `agents/`, `non_agent_count: 0` — identical on both
arms, so the transition is not an artefact of changed discovery scope.

The two STOCK errors, verbatim:

```
DESCRIPTION_EXCESSIVE  "Description is 1468 chars (>1200, 2x the 600-char cap)"
EXAMPLE_BLOCK_PRESENT  "Description has 2 <example> block(s) -- example blocks are
                        rejected entirely, not merely capped"
```

Both clear on the branch. `has_strong_trigger` moves **false → true**
(`triggers_found [] → ["MUST", "DO NOT"]`).

**This is FAIL → PASS WITH WARNINGS, not "PASS held".** Stated plainly because the GOAL asks the
verdict to *stay* PASS: on this repo it did not start there.

### The one remaining warning is pre-existing and deliberately not remediated

`NO_TOOLS_SECTION` — "Agent has no explicit `tools:` section - relying on inheritance". Present
**identically on both arms**; this lane neither introduced nor removed it. It is the sole reason the
branch classifies `needs_work` rather than `good`.

**Not fixed here, on purpose.** Adding a `tools:` block changes frontmatter beyond `description` and
would break this lane's own frontmatter-only fidelity gate. It is also genuinely unsettled: the
recipe's own two LLM passes proposed *different* remedies in the same run — one argued
`tool-skills` **only** (matching `containers:container-operator`, and this repo's README: "This
bundle doesn't ship a runtime … it must be composed onto a bundle that does"), the other argued
`tool-bash` + `tool-filesystem` + `tool-search` + `tool-delegate`. An unadjudicated four-module pin
on an agent whose design assumes composition can *narrow* it silently — an agent that cannot `bash`
cannot run `amplifier-digital-twin launch`, and that failure surfaces at launch time, not at
validation time. **Recorded as a follow-up, not folded in.**

---

## 6. Tests and CI

Run on the branch, after the change:

```
uv run pytest tests/unit/ -q --tb=short      ->  303 passed in 5.86s
uvx ruff@0.16.6 check --isolated --select E4,E7,E9,F .   ->  All checks passed!
```

Both are the repo's own standing commands, verbatim from `.github/workflows/ci.yml`. The repo **has
CI** (added at `50c5d37`): `lint`, `test` on Python 3.11/3.12/3.13, and `bundle-structure`.

### CI caught a real lint failure this lane's own local run had missed — recorded, not smoothed over

The first CI run on PR #36 was **Lint fail**, everything else pass. Cause: the local `ruff` run above
was executed **before** `evidence/census.py` was written, so it never saw the file. `census.py`
opened `import hashlib, json, subprocess, sys` — ruff `E401`, inside the workflow's own
`--select E4,E7,E9,F` tier, and `docs/` is **not** excluded from the lint job (only from
`validate-agents`' discovery). Split onto one-import-per-line, re-verified locally, and the census
re-run afterwards reproduces byte-for-byte (`1468 -> 598`, `body_byte_identical: true`). Fixed in a
follow-up commit on this branch.

Two things worth keeping from that: the lint gate covers lane evidence too, and **a local check run
before the last file is added has not checked that file** — the ordering, not the command, was the
defect.

`tests/unit/test_lean_head_guardrail.py` — which pins `context/dtu-awareness.md` to the measured V1
lean head — is untouched and still green. This change is to `agents/`, a different head surface
(the delegate catalog), so the two do not interact.

**No new guardrail test was added for the description cap.** The cap is already enforced externally
and repo-independently by `validate-agents` v1.8.0 (`WARN >600`, `ERROR >1200`), whose thresholds
are themselves cross-pinned against `validate-bundle-repo.yaml` by foundation's own
`tests/test_description_alignment_checks.py`. A local duplicate would be a second copy that can
drift from the first — V1's failure mode.

---

## 7. Nothing was left compliant-and-unedited

The repo ships exactly one delegate-catalog agent and it was non-compliant on both structural gates.
There is no "already compliant, left alone" entry for this repo.

---

## 8. Census safety

The GOAL's tripwire was honoured: **no `amplifier` invocation with a scratch `AMPLIFIER_HOME`**.
Every measurement is static (reading description strings from the agent source) or a recipe run in
this session's own environment. The `.pth` check result is recorded in `evidence/pth-check.txt`.

---

## 9. Deliverable status

| Deliverable | Status |
|---|---|
| Description trigger-first, ≤~600 chars, USE WHEN / DO NOT USE WHEN, zero example/commentary blocks | **DONE** — 598 chars, `triggers_found ["MUST","DO NOT"]`, `example_count 0` |
| Fidelity table per agent | **DONE** — §4, zero facts lost |
| Bodies byte-identical, md5 both sides | **DONE** — §3, `dd26089828b867a15a3ca91c6f906af4` both |
| Before/after char counts per agent + repo total, vs CURRENT origin/main | **DONE** — §2, 1,468 → 598 (−870, −59.3 %) |
| `validate-agents` run on the branch, verdict quoted, agent count quoted | **DONE** — §5, PASS WITH WARNINGS, 1 agent |
| CI green where the repo has CI | **DONE** — repo has CI; local equivalents green, PR state in the PR body |
| Anything already compliant left unedited and named | **N/A** — §7, nothing in this repo was already compliant |
