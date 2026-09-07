# Copyright (c) Microsoft. All rights reserved.

"""SCRATCH -- deliberate CI failure, proving the gate can go red.

This file exists only on the throwaway branch ci/red-proof-j1e6. It is
never merged: the branch is closed and deleted once a RED run has been
observed. See docs/lanes/j1e6-ci-dtu/DONE-NOTE.md.

Two deliberate defects, one per job:

  * ``os`` is imported and never used  -> ruff F401, so the Lint job goes
    red inside the pinned rule set (E4,E7,E9,F), not by a setup error.
  * ``test_ci_can_go_red`` asserts something false -> the Tests job goes
    red with a genuine TEST failure, inside a suite that collected and
    executed the repo's real 303 tests alongside it.
"""

import os  # noqa: I001  (deliberate F401 -- see module docstring)


def test_ci_can_go_red():
    """Deliberately false. A CI never seen red is decoration."""
    assert 1 == 2, "deliberate failure: proving the Tests job can go red"
