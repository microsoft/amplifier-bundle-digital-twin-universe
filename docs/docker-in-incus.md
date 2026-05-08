# Running Docker Inside a Digital Twin Universe Environment

Some applications spawn Docker containers as part of their normal operation.
When these applications run inside a Digital Twin Universe environment, Docker
runs nested inside Incus — which requires specific host configuration.

This guide covers what's needed, what can go wrong, and how to fix it.

## Quick Version

If you just want it working:

```bash
amplifier-digital-twin launch docker-in-incus --var PORT=8080
curl http://localhost:8080   # => nginx welcome page
```

The `docker-in-incus` profile sets `security.nesting: "true"` via `base.config`,
so no host-level Incus profile change is required. If that works, you're done.
If not, read on.


## Reference Profile

The [docker-in-incus](../profiles/tests/docker-in-incus.yaml) profile is a minimal
test that exercises the full nested networking path:

```
Host -> Incus proxy device -> Incus container :8080 -> Docker bridge -> nginx :80
```

It installs Docker Engine, starts `dockerd` manually (since systemd is not
PID 1 inside Incus), runs `nginx:alpine`, and forwards a parameterized port.
The profile requires no API keys or external services.


## What `security.nesting=true` Does

Incus containers are unprivileged by default — they cannot create
sub-namespaces, which is exactly what Docker needs to do. The
`security.nesting=true` setting relaxes this, allowing `dockerd` inside the
container to create its own cgroups and network namespaces.

Profiles can set this via `base.config`:

```yaml
base:
  image: ubuntu:24.04
  config:
    security.nesting: "true"
```

This passes `--config security.nesting=true` to `incus launch`, so no
host-level Incus profile change is needed. The `docker-in-incus` reference
profile already includes this.

Alternatively, you can set it on the host's default Incus profile (applies to
all future containers, including those without `base.config`):

```bash
incus profile set default security.nesting=true
```


## Platform-Specific Issues

For symptoms and fixes covering Docker + Incus networking conflicts (WSL2,
macOS/Colima, bare-metal iptables), AppArmor + Zabbly upgrade for
docker-in-incus, and Incus group permissions, and more see
[troubleshooting.md](troubleshooting.md)


## Networking Paths

A Digital Twin Universe environment with Docker nesting supports three
networking paths. All three are verified by the
[e2e test](../tests/test_e2e_docker_in_incus.py).

### Host -> Docker (inbound)

```
Host :PORT -> Incus proxy device -> Container :8080 -> Docker bridge -> nginx :80
```

Configured via `access.ports` in the profile. The DTU CLI creates Incus proxy
devices that forward from the host port to the container port.

### Docker -> Host (outbound)

```
Docker container -> docker0 bridge -> Incus container -> Incus bridge (incusbr0) -> Host
```

Docker containers inside Incus reach the host via the Incus container's
default gateway. Discover it with:

```bash
# Inside the Incus container:
ip route | grep default | awk '{print $3}'
```

This is how Docker containers inside the environment reach host-side services
like Gitea or APIs running on the host.

### Docker -> Docker (inter-container)

```
Docker container A -> docker0 bridge (172.17.0.1) -> Docker container B
```

Docker containers within the same Incus container communicate through the
Docker bridge network, just as they would on a regular Docker host. Use the
bridge gateway IP (`172.17.0.1`) with mapped ports, or create a custom Docker
network for DNS-based container name resolution.

## Verification

Run the e2e test to verify all networking paths:

```bash
uv run pytest tests/test_e2e_docker_in_incus.py --run-e2e -v -s
```

Or launch the profile manually:

```bash
amplifier-digital-twin launch docker-in-incus --var PORT=8080
curl http://localhost:8080
amplifier-digital-twin destroy <id>
```
