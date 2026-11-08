# Copyright (c) Microsoft. All rights reserved.
"""Thin designated-operator adapter. Invoke the installed module with python -I."""
import json

import click

from . import admission, bound_backend
from . import legacy_disposition as legacy


@click.command()
@click.argument("operation", type=click.Choice(["prepare", "apply", "reconcile", "recover", "capacity"]))
@click.argument("request_id", required=False)
@click.option("--owner-root")
@click.option("--effect", type=click.Choice([legacy.KEEP, legacy.RELEASE]))
@click.option("--digest")
@click.option("--operation-id")
def main(operation, request_id, owner_root, effect, digest, operation_id):
    try:
        if operation == "capacity":
            result = legacy.capacity()
        elif operation == "prepare":
            result = legacy.prepare(request_id, owner_root, effect)
        elif operation == "apply":
            result = legacy.apply(request_id, owner_root, effect, digest, operation_id)
        elif operation == "recover":
            result = legacy.recover(request_id, digest, operation_id)
        else:
            result = legacy.reconcile(request_id)
        click.echo(json.dumps(result))
    except admission.AdmissionError as exc:
        click.echo(json.dumps(exc.as_dict()))
        raise SystemExit(1)
    except bound_backend.BackendFence:
        click.echo(json.dumps({"error": "backend_fence", "message": "Bound backend unavailable or changed; no backend mutation attempted"}))
        raise SystemExit(1)
    except (OSError, ValueError, KeyError, TypeError):
        click.echo(json.dumps({"error": "disposition_unknown", "message": "Protected disposition could not be verified"}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()