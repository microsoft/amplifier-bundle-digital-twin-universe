# Copyright (c) Microsoft. All rights reserved.
"""Fixed private worker entry point. Never accepts a command, import or env map."""
import json
import os
from pathlib import Path
import sys


def main():
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        raise SystemExit(2)
    # Exact package tree containing this installed/source entry point only.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from amplifier_bundle_digital_twin_universe import admission, collector, collected_launch
    fd = int(sys.argv[1])
    with os.fdopen(fd) as stream:
        admission._private_file(os.fstat(stream.fileno()))
        payload = json.loads(stream.read(262145), object_pairs_hook=collector._unique_object)
    os.environ["AMPLIFIER_DTU_ADMISSION_DIR"] = str(Path(payload["root"]).parent)
    os.environ["AMPLIFIER_DTU_INCUS_LAUNCH_TIMEOUT_SECONDS"] = str(payload["launch_timeout"])
    collected_launch.worker_run(payload)


if __name__ == "__main__":
    main()
