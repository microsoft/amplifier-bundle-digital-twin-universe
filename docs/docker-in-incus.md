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

### WSL2: Docker Blocks Incus Networking

Docker sets the kernel's iptables FORWARD chain to DROP, which can block Incus
bridge traffic (`incusbr0`).

**Symptoms:** `apt-get update` fails inside containers, containers cannot
reach external hosts.

**Fix (one-time):**

```bash
echo '{"ip-forward-no-drop": true}' | sudo tee /etc/docker/daemon.json
```

Then from PowerShell: `wsl --shutdown`, and restart WSL.

> **Note:** On our WSL2 test environment (Incus 6.0.0, Docker CE 28.x),
> Docker nesting worked without this fix. However, this is the documented
> fix for the iptables conflict when it does occur.


### Bare-Metal Ubuntu: iptables Rules May Be Needed

On bare-metal Ubuntu (not WSL2), the `daemon.json` fix alone may not be
sufficient. If networking issues persist after applying the fixes above,
try adding explicit iptables rules for the Incus bridge:

```bash
# Tell Docker not to drop forwarded traffic
sudo python3 -c "
import json, pathlib
p = pathlib.Path('/etc/docker/daemon.json')
d = json.loads(p.read_text()) if p.exists() else {}
d['ip-forward-no-drop'] = True
p.write_text(json.dumps(d, indent=2))
"
sudo systemctl restart docker

# Allow Incus bridge traffic through Docker's DOCKER-USER chain
sudo iptables -I DOCKER-USER -i incusbr0 -j ACCEPT
sudo iptables -I DOCKER-USER -o incusbr0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
```

To persist the iptables rules across reboots, add them to `/etc/rc.local` or
use `iptables-persistent`.


### AppArmor Blocks Docker-in-Docker (Incus < 6.0.6 LTS / < 6.19)

On some bare-metal Ubuntu configurations, AppArmor blocks Docker from
starting inside an Incus container. This is a known issue affecting Incus
versions older than 6.0.6 LTS (or 6.19 on the feature track) with
runc >= 1.2.8.

**Symptoms:** `dockerd` fails to start inside the container with AppArmor
permission denied errors related to `/proc/sys/` or `/sys/`.

**Fix:** Upgrade Incus. The fix shipped in Incus 6.19 and was backported to
6.0.6 LTS. If you are on Ubuntu 24.04 and `apt install incus` gives you
6.0.0, you need to install from the Zabbly repository:

```bash
# Add the Zabbly repo
curl -fsSL https://pkgs.zabbly.com/key.asc | sudo gpg --dearmor -o /etc/apt/keyrings/zabbly.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/zabbly.gpg] \
  https://pkgs.zabbly.com/incus/stable $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/zabbly-incus.list
sudo apt update
```

**Important:** If Ubuntu ESM is enabled, it pins Incus 6.0.0 at apt priority
510. You must specify the Zabbly version explicitly with all three packages:

```bash
ZABBLY_VERSION=$(apt-cache madison incus | grep zabbly | head -1 | awk '{print $3}')
sudo apt install incus=$ZABBLY_VERSION incus-base=$ZABBLY_VERSION incus-client=$ZABBLY_VERSION
sudo systemctl restart incus
```

> **Note:** WSL2 environments do not appear to be affected by this AppArmor
> issue. Docker-in-Incus worked on WSL2 with Incus 6.0.0 without needing
> the Zabbly upgrade.


### Incus Group Permissions

After installing Incus, your user needs the `incus-admin` group.

**Symptom:** `incus version` shows `Server version: unreachable`.

```bash
sudo usermod -aG incus-admin $USER
```

**Gotcha:** Group membership requires a full session restart. `newgrp
incus-admin` only affects the current shell — any process spawned before the
group change (tmux sessions, Amplifier sessions, background services) will
still lack the group. Log out and back in, or reboot.


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
