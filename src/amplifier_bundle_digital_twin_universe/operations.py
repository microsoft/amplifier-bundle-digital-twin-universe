# Copyright (c) Microsoft. All rights reserved.

"""One-shot local Incus creation with a recoverable operation identity.

The Incus CLI's query command can retry a POST after a malformed response.
Use a single Unix-socket HTTP request for the bounded offline/local-image path.
Remote/image-alias launches retain their existing CLI implementation.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import re
import socket
import time
from urllib.parse import parse_qs, quote, urlsplit

from . import incus


class CreationRejected(incus.IncusError):
    """Creation was not submitted, or was explicitly refused before admission."""


class OperationUnknown(incus.IncusError):
    """Transport or identity evidence cannot establish an outcome."""


class _DeadlineSocket(socket.socket):
    """Keep streamed headers/bodies from extending a request's time budget."""

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Incus observation time budget exhausted")
        self.settimeout(remaining)

    def recv_into(self, *args):
        self._remaining()
        return super().recv_into(*args)

    def sendall(self, *args):
        self._remaining()
        return super().sendall(*args)


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = _DeadlineSocket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.deadline = time.monotonic() + self.timeout
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def _request(endpoint: dict, method: str, path: str, data=None, *, timeout=10):
    from .bound_backend import current, BackendFence
    bound = current()
    if bound is not None:
        from .collected_launch import check_cancel
        check_cancel()
        expected = bound.as_dict()
        if (endpoint.get("socket") != expected["socket"]
                or any(endpoint.get(k) != bound.scope[k] for k in ("daemon", "project"))):
            raise BackendFence("Direct transport differs from the bound backend")
        bound.verify()
    separator = "&" if "?" in path else "?"
    path += separator + "project=" + quote(endpoint["project"], safe="")
    connection = _UnixConnection(endpoint["socket"], timeout)
    try:
        body = json.dumps(data).encode() if data is not None else None
        try:
            connection.connect()
        except OSError as exc:
            if method == "POST":
                raise CreationRejected("Could not connect before submitting creation") from exc
            raise
        connection.request(method, path, body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise OperationUnknown("The Incus operation response exceeded its bound")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise OperationUnknown("The Incus operation response was invalid")
        return response.status, value
    finally:
        connection.close()


def _verify_endpoint(endpoint: dict, *, timeout=10):
    status, result = _request(endpoint, "GET", "/1.0", timeout=timeout)
    metadata = result.get("metadata")
    environment = metadata.get("environment") if isinstance(metadata, dict) else None
    if (status != 200 or result.get("type") != "sync"
            or not isinstance(environment, dict)
            or environment.get("certificate_fingerprint") != endpoint["daemon"]
            or environment.get("project") != endpoint["project"]):
        raise OperationUnknown("The Incus daemon or project no longer matches")
    return metadata


def local_endpoint(ctx: dict) -> dict | None:
    """Select only the CLI's current local Unix daemon, without changing it."""
    if "instance_create_start" not in incus.query("/1.0").get("api_extensions", []):
        return None
    selected = incus._run(["incus", "remote", "get-default"], capture_output=True, text=True, timeout=10)
    remotes = incus._run(["incus", "remote", "list", "--format=json"], capture_output=True, text=True, timeout=10)
    if selected.returncode or remotes.returncode:
        raise OperationUnknown("The current Incus endpoint could not be inspected")
    remote = json.loads(remotes.stdout).get(selected.stdout.strip(), {})
    if not isinstance(remote, dict):
        return None
    # Current Incus emits Addrs; older clients emitted Addr. Only one
    # unambiguous address may select the direct transport. Never choose the
    # first of several endpoints or ignore contradictory legacy metadata.
    if "Addrs" in remote:
        addresses = remote["Addrs"]
        if not isinstance(addresses, list) or len(addresses) != 1:
            return None
        address = addresses[0]
        if "Addr" in remote and remote["Addr"] != address:
            return None
    else:
        address = remote.get("Addr")
    if not isinstance(address, str) or not address.startswith("unix://"):
        return None
    path = address.removeprefix("unix://") or os.environ.get("INCUS_SOCKET")
    if not path:
        path = str(Path(os.environ.get("INCUS_DIR", "/var/lib/incus")) / "unix.socket")
    if not Path(path).is_absolute():
        raise OperationUnknown("The Incus socket path must be absolute")
    endpoint = {"socket": path, "daemon": ctx["daemon"], "project": ctx["project"]}
    metadata = _verify_endpoint(endpoint)
    return endpoint if "instance_create_start" in metadata.get("api_extensions", []) else None


def _operation(metadata, name: str, project: str, expected_id=None) -> dict:
    if not isinstance(metadata, dict):
        raise OperationUnknown("Incus did not identify its creation operation")
    identity = metadata.get("id", "")
    if (not isinstance(identity, str)
            or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", identity)
            or expected_id is not None and identity != expected_id
            or metadata.get("class") != "task"):
        raise OperationUnknown("The Incus operation identity did not match")
    resources = metadata.get("resources")
    resources = resources.get("instances") if isinstance(resources, dict) else None
    if not isinstance(resources, list) or len(resources) != 1 or not isinstance(resources[0], str):
        raise OperationUnknown("The Incus operation resource did not match")
    resource = urlsplit(resources[0])
    if (resource.scheme or resource.netloc or resource.fragment
            or resource.path != "/1.0/instances/" + quote(name, safe="")
            or parse_qs(resource.query) not in ({}, {"project": [project]})):
        raise OperationUnknown("The Incus operation resource did not match")
    status = metadata.get("status_code")
    if type(status) is not int or status not in {100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 200, 400, 401}:
        raise OperationUnknown("The Incus operation status was invalid")
    return {"id": identity, "status_code": status}


def submit(endpoint: dict, name: str, image: str, config: dict, pool: str) -> dict:
    """Create and start once. Never retry a malformed/lost submission response."""
    if not re.fullmatch(r"local:[a-f0-9]{64}", image):
        raise ValueError("Tracked creation requires an exact local image")
    try:
        _verify_endpoint(endpoint)
    except (OSError, ValueError, KeyError, TypeError, incus.IncusError, http.client.HTTPException) as exc:
        raise CreationRejected("The endpoint changed before creation was submitted") from exc
    status, result = _request(endpoint, "POST", "/1.0/instances", {
        "name": name, "type": "container", "start": True, "profiles": [],
        "source": {"type": "image", "fingerprint": image[6:]},
        "config": {key: str(value) for key, value in config.items()},
        "devices": {"root": {"type": "disk", "path": "/", "pool": pool}},
    }, timeout=30)
    # A structured synchronous client rejection confirms no async operation.
    # A transport failure, server error or malformed response remains unknown.
    if (status in {400, 401, 403, 404, 409, 412, 422}
            and result.get("type") == "error" and result.get("error_code") == status
            and not result.get("operation")):
        raise CreationRejected("Incus refused the creation request")
    if status != 202 or result.get("type") != "async":
        raise OperationUnknown("Incus did not confirm creation admission")
    operation = _operation(result.get("metadata"), name, endpoint["project"])
    path = urlsplit(result.get("operation", ""))
    if (path.scheme or path.netloc or path.fragment
            or path.path != "/1.0/operations/" + operation["id"]
            or parse_qs(path.query) not in ({}, {"project": [endpoint["project"]]})):
        raise OperationUnknown("Incus returned a different creation operation")
    return {**operation, **endpoint, "name": name,
            "request_id": config["user.dtu.request-id"],
            "owner_root": config["user.dtu.owner-root"]}


def validate_binding(operation: dict, ctx: dict, receipt: dict) -> str:
    if not isinstance(operation, dict) or any(operation.get(key) != value for key, value in {
        "daemon": ctx["daemon"], "project": ctx["project"],
        "name": receipt["name"], "request_id": receipt["request_id"],
        "owner_root": receipt["owner_root"],
    }.items()):
        raise OperationUnknown("The recorded creation operation ownership changed")
    identity = operation.get("id", "")
    if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", identity):
        raise OperationUnknown("The recorded creation operation identity is invalid")
    return identity


def observe(operation: dict, ctx: dict, receipt: dict, *, timeout=2.0) -> dict:
    """Observe exactly the admitted operation; 404 is unknown, never success."""
    identity = validate_binding(operation, ctx, receipt)
    deadline = time.monotonic() + timeout
    _verify_endpoint(operation, timeout=timeout)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Incus observation time budget exhausted")
    status, result = _request(operation, "GET", "/1.0/operations/" + identity, timeout=remaining)
    if status != 200 or result.get("type") != "sync":
        raise OperationUnknown("The recorded creation operation is unavailable")
    return _operation(result.get("metadata"), receipt["name"], ctx["project"], identity)
