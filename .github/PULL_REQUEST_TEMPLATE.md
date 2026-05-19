## Summary
<!-- One-paragraph description of what this PR changes and why. -->

## Verification checklist

- [ ] Unit tests pass (`pytest tests/unit/`)
- [ ] Live DTU test exercising changed operation — required when touching `incus.py`, `engine.py`, provisioning, or file push/pull; paste `incus launch` output and operation result in the section below. Run `incus delete --force <name>` afterward to confirm idempotency.
- [ ] AGENTS.md reviewed; repo-specific gates met
- [ ] Backward-compat path unchanged (if applicable)
- [ ] If file push/pull touched: tested with a directory (not just a file)
- [ ] PR body includes verification evidence, not just "tests pass"

## Verification evidence
<!-- Paste `incus launch` output, operation result, or other proof here. For provisioning changes, include the tail of the launch log. -->

## Notes for reviewers
<!-- Anything reviewers should know — caveats, follow-ups, breaking changes, etc. -->

---
See: [Per-Repo Conventions](https://github.com/microsoft/amplifier-foundation/blob/main/docs/PER_REPO_CONVENTIONS.md) and this repo's `AGENTS.md` for the verification discipline this checklist enforces.
