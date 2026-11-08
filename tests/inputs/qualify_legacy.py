"""Run the focused legacy candidate checks in the owned guest, retain raw output."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--source", type=Path, required=True)
p.add_argument("--output", type=Path, required=True)
p.add_argument("--boundary", action="store_true")
a = p.parse_args()
a.output.mkdir(parents=True, exist_ok=False)
env = dict(os.environ, PYTHONPATH=str(a.source / "src"), PATH="/opt/fixture/venv/bin:/usr/bin:/bin")
steps = []
commands = [
    [sys.executable, "-m", "ruff", "check", "src/amplifier_bundle_digital_twin_universe/legacy_disposition.py", "src/amplifier_bundle_digital_twin_universe/legacy_cli.py", "tests/unit/test_legacy_disposition.py", "tests/inputs/run_legacy_boundary.py", "tests/inputs/qualify_legacy.py"],
    [sys.executable, "-m", "pytest", "tests/unit/test_legacy_disposition.py", "tests/unit/test_admission.py", "tests/unit/test_collected_launch.py", "-q", "--tb=short", "--junitxml=" + str(a.output / "junit.xml")],
    [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(a.output / "dist")],
]
if a.boundary:
    commands.append([sys.executable, str(a.source / "tests/inputs/run_legacy_boundary.py"), "--source", str(a.source), "--wheel", str(a.output / "dist/amplifier_bundle_digital_twin_universe-0.4.3-py3-none-any.whl"), "--output", str(a.output / "boundary")])
for n, command in enumerate(commands):
    result = subprocess.run(command, check=False, cwd=a.source, env=env, capture_output=True, text=True, timeout=240)
    (a.output / f"{n}.stdout").write_text(result.stdout)
    (a.output / f"{n}.stderr").write_text(result.stderr)
    steps.append({"argv": command, "returncode": result.returncode, "stdout_tail": result.stdout[-6000:], "stderr_tail": result.stderr[-2000:]})
    if result.returncode:
        break
(a.output / "result.json").write_text(json.dumps(steps, indent=2) + "\n")
print(json.dumps(steps))
sys.exit(1 if any(s["returncode"] for s in steps) else 0)
