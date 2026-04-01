# Mock Service Authoring Guide

Mock services are standalone servers that mock external APIs inside a
Digital Twin Universe environment. mitmproxy intercepts traffic to the real
domains and routes it to the mock, so code running inside the environment
doesn't need any changes.
The goal is not to replace an API, but provide enough scaffolding to be able to test it 
in a Digital Twin without unnecessary requests being made and having to manually provision them.

## Minimal Requirements

A mock needs three things:

1. **`digital-twin-mock.yaml`** at the repo root
2. **A `Dockerfile`** that builds and runs the server
3. **A server** that listens on the declared port and responds to the target API


### 1. Manifest

```yaml
name: my-service
version: 0.1.0
description: Mock for SomeAPI

runtime:
  type: docker
  port: 3000            # container port the server listens on

domains:                # optional -- hostnames mitmproxy will intercept
  - api.example.com
```

See [profiles.md](profiles.md#mock-service-manifest) for the full manifest reference.

`domains` is optional. Without it the mock still runs as a sidecar but
mitmproxy won't intercept any traffic for it -- useful when the consumer
configures the endpoint URL directly.

### 2. Dockerfile

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -e .
EXPOSE 3000
CMD ["my-server"]
```

Any language and base image works. The only hard requirement is that the
container listens on the port declared in the manifest.

### 3. Server

The server must accept HTTP requests on the declared port.  Beyond that,
what it responds to depends entirely on the API being mocked. A minimal
mock might implement just the two or three endpoints the consumer actually
calls.

### Configuration

Profiles can pass config to mocks:

```yaml
mock_services:
  - source: /path/to/mock
    config:
      workspace_name: "test"
```

Config keys are uppercased and set as environment variables in the
container (`workspace_name` becomes `WORKSPACE_NAME`).

## Optional Patterns

These are not required but make mocks significantly more useful.

**Control API** -- endpoints under `/mock/*` for test automation:
- `/mock/state` -- inspect internal state (connections, stored data)
- `/mock/send-message` -- simulate user actions from the outside
- Useful for E2E tests and for the web UI

**Web UI** -- a simple HTML page served at `/` for human interaction.
Lets someone open a browser and poke around without writing curl commands.

**WebSocket support** -- if the target API uses WebSockets (e.g. Slack
Socket Mode), the mock needs a WebSocket endpoint. mitmproxy transparently
proxies WebSocket upgrades for declared domains.

**URL rewriting caveat** -- if the mock returns URLs referencing itself
(e.g. `ws://localhost:3000/ws`), mitmproxy's response hook rewrites them
to use the real domain (`wss://api.example.com/ws`). The mock doesn't need
to handle this, but it helps to know it happens.

**Sample DTU profiles** -- mocks can ship sample profiles in a `profiles/`
directory showing how to use the mock in a Digital Twin Universe environment.
This gives consumers a working starting point they can launch directly or
copy and adapt.

## Community Mocks

| Mock | Repo |
|------|------|
| Slack | [DavidKoleczek/digital-twin-mock-slack](https://github.com/DavidKoleczek/digital-twin-mock-slack) |
