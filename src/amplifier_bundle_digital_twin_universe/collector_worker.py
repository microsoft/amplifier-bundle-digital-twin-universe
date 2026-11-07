# Copyright (c) Microsoft. All rights reserved.
"""Fixed private worker entry point. No arbitrary command/import/environment input."""
import hashlib
import importlib.abc
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys


class VerifiedPackage(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Compile the same bytes whose hash was bound; never use a later file read.

    This prevents a cold engine import from using changed source after bootstrap
    verification. It is not protection from an administrator replacing this
    interpreter/entry point or malicious same-UID code inside the process.
    """
    prefix = "amplifier_bundle_digital_twin_universe"

    def __init__(self, package, files):
        self.package, self.files = package, files

    def find_spec(self, fullname, path=None, target=None):
        if fullname == self.prefix:
            name = "__init__.py"
        elif fullname.startswith(self.prefix + ".") and fullname.count(".") == 1:
            name = fullname.rsplit(".", 1)[1] + ".py"
        else:
            return None
        if name not in self.files:
            raise ImportError("Unbound worker module")
        spec = importlib.util.spec_from_file_location(fullname, self.package / name, loader=self,
                    submodule_search_locations=[str(self.package)] if name == "__init__.py" else None)
        return spec

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        path = Path(module.__spec__.origin)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != self.files[path.name]:
            raise ImportError("Worker module changed before import")
        exec(compile(raw, str(path), "exec"), module.__dict__)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate bootstrap key")
        result[key] = value
    return result


def main():
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        raise SystemExit(2)
    fd = int(sys.argv[1])
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 262144):
            raise SystemExit(2)
        payload = json.loads(stream.read(262145), object_pairs_hook=_unique)
    runtime = payload["runtime"]
    package = Path(__file__).resolve().parent
    if (str(Path(sys.executable).absolute()) != runtime["python"]
            or hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest() != runtime["python_digest"]
            or hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != runtime["files"].get("collector_worker.py")):
        raise SystemExit(2)
    sys.meta_path.insert(0, VerifiedPackage(package, runtime["files"]))
    from amplifier_bundle_digital_twin_universe import collected_launch, worker_guard
    os.environ["AMPLIFIER_DTU_ADMISSION_DIR"] = str(Path(payload["root"]).parent)
    os.environ["AMPLIFIER_DTU_INCUS_LAUNCH_TIMEOUT_SECONDS"] = str(payload["launch_timeout"])
    backend = collected_launch.restore_backend(payload["backend"])
    report = collected_launch._input_path(Path(payload["root"]), payload["binding"]["request_id"]).with_suffix(".guard")
    retain = worker_guard.install(backend, report)
    try:
        collected_launch.worker_run(payload)
    finally:
        retain()


if __name__ == "__main__":
    main()
