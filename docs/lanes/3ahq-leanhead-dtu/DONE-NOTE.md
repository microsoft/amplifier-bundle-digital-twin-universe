# DONE-NOTE — lane `3ahq-leanhead-dtu`

**Item:** `model_performance-aj2j` — *lean head repo 9/13 amplifier-bundle-digital-twin-universe: dtu-awareness.md — 407 ch, patch ready*
**Repo:** `microsoft/amplifier-bundle-digital-twin-universe`
**Branch:** `lane/3ahq-leanhead-dtu` · **Draft PR:** [#33](https://github.com/microsoft/amplifier-bundle-digital-twin-universe/pull/33)
**Date:** 2026-09-07
**Outcome:** **A — RESOLVED.** Every deliverable DONE. The cap never bound; nothing needed buying.

---

## 1. Result in one line

`context/dtu-awareness.md` **1,093 → 686 chars (−407, −37.2%)**, byte-identical to the measured V1 text, with a guardrail that **fails against the pre-change file** and passes after — landed as a draft PR, **not merged**.

---

## 2. Deliverables

| # | Deliverable | State | Evidence |
|---|---|---|---|
| 1 | The patch applied | **DONE** | `git apply` took it **cleanly at today's head** — exact context, **no fuzz, no hand-port**. `29e2414`. |
| 2 | Fidelity table re-verified at today's head | **DONE** | §4 below. 16/16 required-rule assertions + a 17-concept hand audit, re-derived here. **Not inherited** — and §5 explains why inheriting would have been worthless. |
| 3 | Stock → lean char counts | **DONE** | **1,093 → 686 chars** (−407). 688 **bytes** post-apply: one U+2014 EM DASH is 3 bytes. Budget asserted in **chars**; asserting bytes would loosen it by 2. |
| 4 | Byte-for-byte pin test against the v1 text | **DONE** | `tests/unit/test_lean_head_guardrail.py` pins against `tests/fixtures/lean-head-v1/dtu-awareness.md`, a verbatim vendored copy of `v1_instructions.json` span 1's body (686 chars). Verified equal to the upstream `.lean.md` artifact. |
| 5 | CI: this repo has NONE — say so plainly | **DONE (stated plainly)** | `.github/` holds only `PULL_REQUEST_TEMPLATE.md`; **there is no `.github/workflows` directory**. No CI run URL exists for this repo, red or green. §3 gives the local red/green instead, labelled as local. |
| 5b | *(item acceptance criterion)* "both red and green **CI run URLs** quoted in the PR body" | **NOT-POSSIBLE — no CI exists in this repo** | Structural, not cap-driven. **What was executed:** the guardrail was landed in its own commit and run against the pre-change tree — **2 failed, 23 passed**, both failures in the census half — then run again after the change — **25 passed**; full suite **303 passed, 77 skipped**. Both runs are committed verbatim under `evidence/`. What is missing is only the *URL*, because the workflow file it would point at does not exist. This repo is one of the 18 in the queued `j1e6` CI lane, sequenced **after** this one by design. |
| 6 | Draft PR, ready when green, **not merged** | **DONE** | Draft [#33](https://github.com/microsoft/amplifier-bundle-digital-twin-universe/pull/33). Left **draft** deliberately: with no CI in this repo there is no green *CI* signal to mark ready on, and the manager merges. |
| 7 | DONE-NOTE at the lane-artifact root | **DONE** | This file, at `docs/lanes/3ahq-leanhead-dtu/` — **never** the repo root (item `kez`). |

---

## 3. Verification

| Stage | Command | Result |
|---|---|---|
| **RED** — guardrail alone, `ef1fca6` | `pytest tests/unit/test_lean_head_guardrail.py -v` | **2 failed, 23 passed** |
| **GREEN** — after the change, `29e2414` | `pytest tests/unit/test_lean_head_guardrail.py -q` | **25 passed** |
| Full suite | `pytest tests/ -q` | **303 passed, 77 skipped** |
| Lint | `ruff check tests/unit/test_lean_head_guardrail.py` | `All checks passed!` |

The two RED failures are **exactly** the census half, and that is the designed shape:

```
FAILED TestBytePinAgainstTheV1Head::test_dtu_awareness_is_byte_identical_to_v1
FAILED TestHeadCensus::test_context_file_within_budget
E  AssertionError: context/dtu-awareness.md is 1093 chars, over its pinned lean budget of 686 (+407).
E  assert 1093 <= 686
```

All 16 fidelity assertions **pass on the stock text too**. That is correct, not a weak test: nothing had been dropped yet, so the fidelity half has nothing to catch until someone tries to buy bytes with an instruction.

77 skipped = `integration` + `e2e`, gated by this repo's own `conftest.py` behind `--run-integration` / `--run-e2e`. Unchanged by this lane. Per `AGENTS.md`'s verification gradient a doc/context change is "unit tests sufficient" — no `incus.py` / `engine.py` / provisioning / file-push-pull code is touched, so no live DTU launch is implicated. **No DTU was launched; no infrastructure was created; the infra ledger has no rows from this lane.**

Scope, diffed against the **merge-base** `755a143` (not a moved `origin/main` — the goal names that trap explicitly): 5 files, all inside this repo, all inside the paths this lane owns.

---

## 4. Fidelity table — re-verified at this head

Every rule, constraint, command and pointer in the stock text survives. **Both commands are byte-identical, not paraphrased.**

| Stock element | Survives | Form in lean |
|---|---|---|
| "Digital Twin Universe" / "DTU" | ✅ | title line, `# Digital Twin Universe (DTU)` |
| on-demand isolated environments | ✅ | verbatim |
| declarative profiles | ✅ | verbatim |
| "as if actually deployed" | ✅ | verbatim |
| isolated env for a realistic deployment setting | ✅ | "realistic-deployment testing" |
| "simulated Amplifier user environment or web UI" | ✅ | verbatim |
| ephemeral container | ✅ | verbatim |
| DNS rewriting · API passthrough · port forwarding | ✅ | verbatim, `,` → `/` |
| local repos tested as if already published | ✅ | "local repos tested as if published" |
| "launch and verify an environment" | ✅ | verbatim |
| "also handles Gitea setup for local repos" | ✅ | verbatim |
| `delegate(agent="digital-twin-universe:dtu-profile-builder", …)` | ✅ | **byte-identical** |
| `load_skill(skill_name="digital-twin-universe")` | ✅ | **byte-identical** |
| fallback conditions: general questions · install help · troubleshooting | ✅ | verbatim |

**Removed:** section headings (`## When to Use`, `## How to Use`), the "You have access to…" preamble, and the bullet/fence markup. **Prose scaffolding only. No instruction was traded for bytes.**

**Expected losses: none. Observed losses: none. Nothing needed restoring, so the byte delta from restoration is 0.**

> The goal warned that this repo carried "the one real weakening `zc6t` found — `edit_file`, restored in-repo at +450 chars". **It does not.** `edit_file` is a tool description in `amplifier-module-tool-filesystem`, a different repo (`fidelity-report.json`, `kind: tool`, `repo: amplifier-module-tool-filesystem`). See §6.

---

## 5. Finding **F-3ahq-1** — the upstream `missing_rules: []` for this target is true but **vacuous**

`docs/lanes/zc6t-lean-head-ship/patches/fidelity-report.json` records `missing_rules: []` for index 1, and the item description repeats it as "Fidelity check on this target: CLEAN". Both are *true*. Neither is *evidence*.

Running `zc6t`'s own `extract_rules()` against this repo's stock text at today's head:

```
CODE_SPAN  pattern='`([^`\n]{2,80})`'  stock_hits=0  lean_hits=1
MENTION                                stock_hits=0  lean_hits=0
URL                                    stock_hits=0  lean_hits=0
CAPS                                   stock_hits=0  lean_hits=0
CMD                                    stock_hits=0  lean_hits=0
→ extracted 0 rules/constraints/commands/pointers from STOCK
→ MISSING COUNT: 0
```

**The extractor matched nothing to lose.** `CODE_SPAN` only matches *inline* backtick spans; the **stock** text puts both of its commands inside **triple-backtick fenced blocks**, which `` `[^`\n]{2,80}` `` cannot span. There are no @mentions, no URLs, no CAPS keywords and no shell commands in either text.

So `missing_rules: []` here means *"the extractor found 0 rules, and 0 of them were missing"* — the same output it would produce for a rewrite that **deleted the file outright**. The direction is also backwards: the **lean** text scores 1 `CODE_SPAN` hit precisely because it converted the fences to inline spans, so the check would only bite in reverse.

**Consequence, and it is the reason this lane's re-verification was not ceremony:** the fidelity claim in §4 rests on 16 hand-written literals asserted in the test plus a 17-concept hand audit, both derived from the stock text at this head. It does **not** rest on that `[]`.

**Generalisation for the manager:** this is the goal's own "confident, plausible, wrong output while exiting 0" pattern, one layer up. Any of the other 12 sibling lanes whose stock text uses fenced blocks rather than inline spans — a common shape for context files that show a command — inherited the same vacuous CLEAN. The report's `missing_rules` field is only meaningful where `extract_rules(stock)` was **non-empty**; where it was empty the field is unfalsifiable. Worth a one-line check per target before any lane quotes "CLEAN" as an inherited result.

*(Where the extractor did fire, it worked: the report's 4 flags across 23 targets — 3 false positives + the genuine `edit_file` loss — all come from targets whose stock text contained matchable tokens.)*

---

## 6. Finding **F-3ahq-2** — `GOAL.md`'s Task section names the wrong repo (goal defect, reported not absorbed)

`GOAL.md` states: *"This lane owns ONLY the `amplifier-module-tool-filesystem` slice: `read_file`, `write_file`, `edit_file`, `grep`, `glob`"*, and further asserts *"THIS REPO CARRIES THE ONE REAL WEAKENING"* (the `edit_file` +450-char restoration).

**Neither is true of this lane.** The claimed item `model_performance-aj2j` and this worktree are `amplifier-bundle-digital-twin-universe`; the target is the context file `context/dtu-awareness.md`; the patch is `patches/context-files/01-…`, not `patches/tool-descriptions/`. `read_file` / `write_file` / `edit_file` / `grep` / `glob` live in a different repository and are a different sibling lane's slice.

**Resolution taken, no human wait:** Procedure 1 is explicit — *"the returned description + acceptance criteria are the authoritative spec; this file summarizes them"* — so the **work item won**, and the lane executed the DTU slice. Every path this lane wrote is inside its own repo and inside its own artifact root. Nothing was written to `amplifier-module-tool-filesystem`.

Most likely cause: paste-through of a sibling `3ahq` lane's Task/Deliverables section into this lane's `GOAL.md`. The two goal sections that are repo-specific (Task, and the "one real weakening" paragraph) were not re-keyed; the sections that are lane-specific (artifact root, lane id, marker path, "CI: this repo has NONE") **were** correct, which is what makes the defect easy to miss on a fast read.

**Cost of the defect, honestly:** ~0. It was caught on the first read by comparing the goal against the claimed item, exactly as the goal's own "verify a claim against a value you already know" note instructs. It is recorded because the *next* lane to inherit a pasted goal may not check, and because a goal that names the wrong five tools would send a less careful lane to write in another repo — the precise failure `zc6t` reported as F1.

---

## 7. Spend

| | |
|---|---|
| Authority | **$0.00** — arithmetic as stated in the goal: **0 runs × 0 arms × $0 / 1.00 = $0.00**, slack $0.00 |
| Spent | **$0.00** |
| API calls bought | **0** |
| DTUs / Gitea / infrastructure created | **none** — no ledger rows registered, nothing to tear down |
| Residue | $0.00 |

**The cap never bound**, so outcome branch B is not in play. The arithmetic closes trivially: this deliverable is *application of an already-measured patch plus a local suite*, and it requires no purchase at all. The $/task answer was bought once by `g7h3` at $428.10 and was **not** re-bought; the quality answer was bought by `5zp` and was **not** re-bought. Both are cited in the guardrail so a later edit cannot quietly loosen the frozen −10 pp margin or swap in a lower bound that no longer clears it.

---

## 8. What remains open

1. **CI for this repo does not exist.** Until `j1e6` lands a workflow, this guardrail runs only when someone runs pytest. That is the single largest gap between what this lane shipped and what it is *for*: a pin that does not execute on every PR is a pin that eventually stops being true.
2. **F-3ahq-1 is a batch-wide question, not a repo-wide one.** Twelve sibling targets may carry the same vacuous CLEAN. One `extract_rules(stock)` call per target answers it for the whole batch.
3. **The PR is draft and must stay that way until the manager decides.** Local green is not CI green, and this lane will not pretend otherwise by marking it ready.

---

## 9. Files this lane touched

```
context/dtu-awareness.md                        1,093 → 686 chars  (the change)
tests/unit/test_lean_head_guardrail.py          new, 250 lines     (the guardrail)
tests/fixtures/lean-head-v1/dtu-awareness.md    new, 686 chars     (the pinned v1 reference)
tests/fixtures/lean-head-v1/README.md           new                (its provenance)
docs/lanes/3ahq-leanhead-dtu/                   new                (this note + evidence)
```

Evidence, verbatim, under `evidence/`:

| File | What |
|---|---|
| `guardrail-local-red.txt` | the full RED run against the pre-change text |
| `guardrail-local-green.txt` | the GREEN run after the change |
| `full-suite-green.txt` | `pytest tests/ -q` |
| `fidelity-reverify-3ahq.txt` | the re-derivation in §4 and the F-3ahq-1 measurement in §5 |
