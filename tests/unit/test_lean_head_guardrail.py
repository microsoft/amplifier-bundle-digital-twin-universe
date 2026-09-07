# Copyright (c) Microsoft. All rights reserved.

"""Guardrail for this bundle's slice of the lean head (`model_performance-aj2j`).

WHAT THIS PROTECTS, AND WHY IT IS WORTH A TEST
----------------------------------------------
`context/dtu-awareness.md` is a bundle *context file*. It is composed into the request
head of **every session that loads this bundle, on every request, whether or not a DTU is
ever launched**. It is not paid per use; it is paid per session, up front.

`model_performance-g7h3` bought 98 end-to-end claude-opus-5 runs (49/arm, $428.10) measuring
the lean head this file is one span of. All three pre-registered estimators excluded zero for
the first time in the program:

    primary      (VALID only, n 11/8)  -13.57%  95% CI [-22.27%, -4.86%]
    ALL runs     (n 49/49)             -16.42%  95% CI [-23.29%,  -9.56%]
    block-paired (7 pairs)             -14.44%  95% CI [-26.83%,  -2.06%]

Co-primary quality is CITED from `model_performance-5zp`: one-sided 95% lower bound -7.15 pp
against the frozen -10 pp non-inferiority margin — CLEARS.

The head is shared between vendors and Anthropic is the daily driver, where the lever is worth
more per request. That value rests on ONE cold head write per session: the head must be
byte-stable so `cache_read` returns to exactly it at every compaction boundary. A head that
silently regrows, or that acquires a per-turn-varying byte, costs that without anything failing.

WHAT EACH ASSERTION CAN AND CANNOT PROVE
----------------------------------------
Written down, because a guardrail whose limits are not stated gets read as proving more than
it does.

(a) BYTE PIN + CHAR BUDGET — fully proven here. An exact char count of a real source file
    against a vendored copy of the measured V1 text. No estimate, no token model. **This is
    the half that goes RED against the pre-change (stock) text**: stock is 1,093 chars against
    a 686-char budget.

(b) ONE COLD HEAD WRITE PER SESSION — the *precondition* is proven here; the cache behaviour
    itself is not. A static repo test cannot observe `cache_read` on a live response. What it
    CAN prove, and what actually breaks the property in practice, is that the text is
    deterministic: no timestamp, no absolute path, no content hash, no PID, no session id.

(c) FIDELITY — proven for the literals listed in `REQUIRED_RULES`, which were re-derived from
    the stock text at THIS repo's head, not inherited. See the note on that list below; it is
    load-bearing and it is the reason this test hand-writes literals instead of trusting an
    upstream extractor.

(d) THE COST AND QUALITY EFFECTS — cited, not re-measured. The item's spend authority is $0
    for exactly that reason. The citation is pinned so it cannot be quietly loosened.

Pure-Python, filesystem-only. No network, no API key, no Incus.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTEXT_FILE = REPO_ROOT / "context" / "dtu-awareness.md"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "lean-head-v1" / "dtu-awareness.md"

# --- (a) the census ---------------------------------------------------------
#
# Chars, not bytes and not tokens — the published head census is in chars, so these can be
# added to it directly. They differ here: the lean text carries one U+2014 EM DASH, so it is
# 686 chars but 688 bytes. Counting bytes would silently loosen the budget by 2.
STOCK_CHARS = 1093  # measured at this repo's head before the change
BUDGET_CHARS = 686  # the measured lean size, pinned — never a round number above it
SAVED_CHARS = 407

# --- (d) the cited effects, frozen -----------------------------------------
NON_INFERIORITY_MARGIN_PP = -10.0  # frozen, SPEC.md 9.1
CITED_QUALITY_LOWER_BOUND_PP = -7.15  # model_performance-5zp, one-sided 95% LB
CITED_COST_DELTA_PCT = -13.57  # model_performance-g7h3 primary
CITED_COST_CI_PCT = (-22.27, -4.86)

# --- (c) rules that must survive the rewrite --------------------------------
#
# "Fidelity beats compression at every point of conflict." A saving bought by deleting a real
# instruction is not a saving; it is a behaviour change with no trace back to the commit that
# caused it.
#
# EVERY LITERAL BELOW WAS RE-DERIVED FROM THE STOCK TEXT AT THIS REPO'S HEAD. It was not
# inherited from the upstream fidelity report, and that distinction is not ceremony:
#
#   `docs/lanes/zc6t-lean-head-ship/patches/fidelity-report.json` records `missing_rules: []`
#   for this target. That is TRUE but VACUOUS. Its extractor harvests inline code spans
#   (`` `x` ``), @mentions, URLs, CAPS keywords and shell commands — and the STOCK text
#   contains none of those: it puts both of its commands inside TRIPLE-BACKTICK FENCED
#   BLOCKS, which the inline-span pattern (`[^`\n]{2,80}`) cannot match. Measured: 0 tokens
#   extracted from stock. An extractor that finds nothing reports nothing missing for ANY
#   rewrite — including one that deleted the file outright.
#
# So the two commands below are asserted as exact literals, because those two lines are the
# entire actionable payload of this context file and a paraphrase of either one is a defect.
REQUIRED_RULES = [
    # what the capability IS
    "Digital Twin Universe",
    "DTU",
    "isolated environments",
    "declarative profiles",
    "as if actually deployed",
    # when to reach for it — every stock bullet
    "simulated Amplifier user environment or web UI",
    "ephemeral container",
    "DNS rewriting",
    "API passthrough",
    "port forwarding",
    "local repos",
    # how to use it — the two commands, verbatim, and their conditions
    "launch and verify an environment",
    "Gitea setup for local repos",
    (
        'delegate(agent="digital-twin-universe:dtu-profile-builder", '
        'instruction="<what the user needs>", context_depth="recent", '
        'context_scope="conversation")'
    ),
    'load_skill(skill_name="digital-twin-universe")',
    "troubleshooting",
]

# --- (b) tokens that would break one-cold-head-write-per-session -------------
#
# Anything whose value can differ between two compositions of the same head.
VOLATILE_PATTERNS = {
    "an absolute POSIX path": re.compile(r"(?<![\w`])/(?:home|root|tmp|Users)/"),
    "a Windows absolute path": re.compile(r"[A-Za-z]:\\\\"),
    "an ISO timestamp": re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}"),
    "a 40-hex sha": re.compile(r"\b[0-9a-f]{40}\b"),
    "a cache content-hash suffix": re.compile(r"-[0-9a-f]{16}\b"),
}


def _shipped() -> str:
    return CONTEXT_FILE.read_text(encoding="utf-8")


def _pinned() -> str:
    return FIXTURE.read_text(encoding="utf-8")


class TestBytePinAgainstTheV1Head:
    """The shipped context file IS the measured V1 text, byte for byte."""

    def test_dtu_awareness_is_byte_identical_to_v1(self) -> None:
        want, got = _pinned(), _shipped()
        assert got == want, (
            "context/dtu-awareness.md has drifted from the V1 lean head "
            f"({len(got)} chars vs the pinned {len(want)}). The -13.57% cost reduction was "
            "measured against these exact bytes.\n"
            f"--- pinned v1 ---\n{want}\n--- shipped now ---\n{got}"
        )

    def test_the_v1_fixture_is_the_measured_size(self) -> None:
        """Guards the reference itself: an edited fixture proves nothing."""
        assert len(_pinned()) == BUDGET_CHARS


class TestHeadCensus:
    """The census stays at or below the shipped lean size.

    THIS is the assertion that fails against the stock text. Run it on the pre-change tree
    and `context/dtu-awareness.md` is 1,093 chars against a 686-char budget.
    """

    def test_context_file_within_budget(self) -> None:
        actual = len(_shipped())
        assert actual <= BUDGET_CHARS, (
            f"context/dtu-awareness.md is {actual} chars, over its pinned lean budget of "
            f"{BUDGET_CHARS} (+{actual - BUDGET_CHARS}). This file is in the head of every "
            "session that loads this bundle, on every request, paid on one cold write per "
            "session. If the growth is a RULE that must be there, raise the budget in the "
            "same commit and say why in the message — never silently."
        )

    def test_the_recorded_saving_has_not_moved(self) -> None:
        """The saving is a number in the repo, not a claim in a commit message."""
        assert STOCK_CHARS - BUDGET_CHARS == SAVED_CHARS, (
            f"The recorded saving moved: {STOCK_CHARS - BUDGET_CHARS} chars, was "
            f"{SAVED_CHARS}. Update the budget, the stock figure and the lane DONE-NOTE "
            "together, or the published census and this repo disagree."
        )


class TestOneColdHeadWritePerSession:
    """The precondition for `cache_read` returning to exactly the head.

    See the module docstring: this proves determinism of the text, not the cache behaviour.
    A volatile token here is the concrete, repo-side cause of a second cold write.
    """

    def test_context_file_carries_no_volatile_token(self) -> None:
        text = _shipped()
        offenders = [
            f"{label}: {match.group(0)!r}"
            for label, pattern in VOLATILE_PATTERNS.items()
            for match in [pattern.search(text)]
            if match
        ]
        assert not offenders, (
            f"context/dtu-awareness.md contains a token that can differ between two "
            f"compositions of the same head: {offenders}. Every such token forces a second "
            "cold head write and forfeits the Anthropic cache advantage this change bought."
        )

    def test_the_file_reads_identically_twice(self) -> None:
        """Composition is a pure function of the source, not of the clock."""
        assert _shipped() == _shipped()


class TestFidelityBeatsCompression:
    """No rule, constraint, command or pointer was traded away for bytes."""

    @pytest.mark.parametrize("rule", REQUIRED_RULES)
    def test_every_required_rule_survived(self, rule: str) -> None:
        # Case-insensitive on purpose: the invariant is that the RULE is still stated, not
        # that a sentence still starts with the same capital. Upstream, a case-sensitive
        # match reported two false losses ("Never push..." vs "never push...") — exactly the
        # noise that teaches a reader to skim past a real one.
        assert rule.casefold() in _shipped().casefold(), (
            f"context/dtu-awareness.md lost this rule/command/pointer: {rule!r}. Fidelity "
            "beats compression at every point of conflict — restore the text and raise the "
            "char budget rather than shipping the saving."
        )


class TestCitedEffects:
    """The bought numbers, pinned so a later edit cannot quietly loosen them."""

    def test_cited_quality_lower_bound_clears_the_frozen_margin(self) -> None:
        assert CITED_QUALITY_LOWER_BOUND_PP > NON_INFERIORITY_MARGIN_PP, (
            f"The cited one-sided 95% LB ({CITED_QUALITY_LOWER_BOUND_PP} pp) no longer "
            f"clears the frozen margin ({NON_INFERIORITY_MARGIN_PP} pp). Shipping the lean "
            "head is not justified without a fresh measurement — and the margin is frozen, "
            "so it is the LB that must move, not the margin."
        )

    def test_the_frozen_margin_has_not_been_loosened(self) -> None:
        assert NON_INFERIORITY_MARGIN_PP == -10.0

    def test_the_cited_cost_effect_excludes_zero(self) -> None:
        """The reason this change ships at all: the CI does not straddle zero."""
        low, high = CITED_COST_CI_PCT
        assert low < CITED_COST_DELTA_PCT < high
        assert high < 0, (
            f"The cited 95% CI {CITED_COST_CI_PCT} no longer excludes zero; the SHIP rule "
            "that fired for this change does not hold."
        )
