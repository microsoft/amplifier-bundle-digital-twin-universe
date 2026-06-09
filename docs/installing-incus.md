# Installing Incus

<!-- Agent instruction:
- Run any command you can run yourself (non-sudo). Only hand commands to
  the user when they require sudo or a terminal restart.
- Before starting, briefly tell the user what you're going to install and
  which commands will need their sudo password.
- Present commands to the user one at a time, each in its own code block.
  Terminal copy-paste breaks with multiline blocks.
- Always specify actions like "close and reopen your terminal" when group
  changes or service restarts require it.
- If the user will use Docker inside Incus (you can ask), route the user to the Zabbly install
  path below on bare-metal Ubuntu. Distro Incus 6.0.0 has an AppArmor bug
  that breaks docker-in-incus and is silently pinned by Ubuntu ESM. -->

Incus is the container runtime used by the Digital Twin Universe.

> **Sources:** Instructions derived from the official Incus install docs at
> https://linuxcontainers.org/incus/docs/main/installing/ and the Colima project
> at https://github.com/abiosoft/colima. Docker coexistence fix from
> https://linuxcontainers.org/incus/docs/main/howto/network_bridge_firewalld/.

## Recommended version

Install a current Incus release (**Incus 7 LTS or newer**) rather than the
distro default. The version in the Ubuntu 24.04 archive is 6.0.0, which has two
bugs that affect DTU usage and are both fixed upstream:

- an AppArmor bug that blocks Docker-in-Incus (fixed in 6.0.6 LTS / 6.19), and
- an image-unpack bug that breaks publishing and reusing base images via
  `incus publish` (fixed in 6.0.1). See
  [troubleshooting.md → Reusable / published images](troubleshooting.md#reusable--published-images).

Install Incus from the upstream packages. Install instructions change over time,
so follow the official sources for the current steps:

- Zabbly packages (Linux): https://github.com/zabbly/incus/blob/daily/README.md
- Incus install docs (all platforms): https://linuxcontainers.org/incus/docs/main/installing/

The platform sections below cover group membership and initialization once Incus
is installed; on bare-metal Ubuntu, prefer the Zabbly path over the distro
package (see [Docker coexistence → Bare-metal Ubuntu](#bare-metal-ubuntu-running-docker-inside-incus-install-from-zabbly)).

### Checking your version

Check the installed client and server versions:

```bash
incus version
```

A fresh install from the sources above gives a current release. An existing
install that already works for your profiles is fine to keep as-is.
Do not proactively tell the user their Incus is old, or
suggest upgrading based on the version number alone — an older version that works
is fine. Only suggest upgrading when the user actually hits one of the issues in troubleshooting.md.

## Ubuntu / WSL2 (Ubuntu 24.04+)

> The Incus package in the Ubuntu 24.04 archive is 6.0.0, which is too old for
> DTU use (see [Recommended version](#recommended-version)). Install a current
> Incus from the upstream Zabbly packages instead of `apt install incus`.

Install Incus from Zabbly. The concrete apt steps (repo key, source list, and
the version pin needed on hosts with Ubuntu ESM) are in
[Docker coexistence → Bare-metal Ubuntu](#bare-metal-ubuntu-running-docker-inside-incus-install-from-zabbly).
For the latest upstream instructions, follow
https://github.com/zabbly/incus/blob/daily/README.md.

Once Incus is installed, add your user to the `incus-admin` group:
```bash
sudo adduser $USER incus-admin
```

Apply the group change (or close and reopen your terminal):
```bash
newgrp incus-admin
```

Initialize Incus with defaults:
```bash
sudo incus admin init --minimal
```

## macOS

The Incus server is Linux-only. On macOS, use Colima to run it inside a
lightweight VM. The host gets the Incus client; the Incus daemon runs inside
the Colima VM and is reached via a forwarded unix socket.

### 1. Install the host CLIs

```bash
brew install incus colima
```

### 2. Start Colima with Incus as the runtime

A 4 CPU / 4 GB / 30 GB sizing is recommended for typical DTU profiles —
smaller sizings can OOM during package installs:
```bash
colima start --runtime incus --cpu 4 --memory 4 --disk 30
```

### 3. Install Incus inside the Colima VM

> **Why this is needed:** As of Colima 0.10.x, the `--runtime incus` flag
> registers the runtime in Colima's profile state but does not install the
> incus binary inside the Lima/Ubuntu VM. Without this step, Colima's
> auto-provisioning step `incus admin init --preseed` fails silently with
> `sudo: incus: command not found` and the VM has no incus daemon. Future
> Colima versions may install incus inside the VM automatically; this step
> is a no-op in that case.

Install Incus inside the VM:
```bash
colima ssh -- sudo apt-get update
colima ssh -- sudo apt-get install -y incus
```

Initialize Incus with defaults:
```bash
colima ssh -- sudo incus admin init --minimal
```

### 4. Allocate uid/gid for root inside the VM

> **Why this is needed:** `incus admin init --minimal` does not populate
> `/etc/subuid` and `/etc/subgid` for `root`, so unprivileged container
> creation fails with `Invalid config: No uid/gid allocation configured.
> In this mode, only privileged containers are supported`.

Open a shell inside the VM:
```bash
colima ssh
```

Then, at the VM prompt:
```bash
echo "root:1000000:1000000000" | sudo tee -a /etc/subuid
echo "root:1000000:1000000000" | sudo tee -a /etc/subgid
sudo systemctl restart incus
exit
```

### 5. Apply the Docker iptables coexistence fix inside the Colima VM

> **Why this is needed:** The Colima VM has Docker pre-installed and
> running, even when `--runtime incus` is selected. Docker sets the kernel's
> `iptables` `FORWARD` policy to `DROP`, which silently blackholes outbound
> traffic from Incus containers (the same issue documented for Linux/WSL2
> below). The Colima VM itself can reach the internet, but Incus containers
> cannot.

Open a shell in the VM:
```bash
colima ssh
```

Then, at the VM prompt:
```bash
echo '{"ip-forward-no-drop": true}' | sudo tee /etc/docker/daemon.json
sudo systemctl restart docker
exit
```

### 6. Configure the host Incus client to use the Colima socket

Colima forwards the in-VM Incus unix socket to
`~/.colima/<profile>/incus.sock` on the host (`<profile>` is `default`
unless you used `colima start --profile`). The host's static `local`
remote uses `unix://` (no path), which resolves to
`/var/lib/incus/unix.socket` — a path that does not exist on macOS — so it
must be supplemented with a non-static remote pointing at the forwarded
socket:

```bash
incus remote add colima "unix://$HOME/.colima/default/incus.sock"
incus remote switch colima
```

After this, `incus version` on the host should report both client and
server versions.

### Prerequisites: Docker (optional)

Docker Desktop on the macOS host is **not** required for Incus itself —
Colima provisions its own Docker engine inside the VM (used internally,
which is why step 5 is needed). Docker on the host is only needed for
profiles that use the Gitea bundle or mock service sidecars on the host
side. If the profile you're launching requires it, see
[installing-docker.md](installing-docker.md).


## Docker coexistence (Ubuntu / WSL2)

If Docker is also installed, it sets an iptables rule that blocks Incus networking. Containers will launch but have no internet.

Apply the fix:
```bash
echo '{"ip-forward-no-drop": true}' | sudo tee /etc/docker/daemon.json
```

Restart Docker:
```bash
sudo systemctl restart docker
```

On WSL2, also restart WSL from PowerShell:
```powershell
wsl --shutdown
```

Then reopen your WSL terminal.

### Bare-metal Ubuntu running Docker inside Incus: install from Zabbly

Distro Incus on Ubuntu 24.04 is 6.0.0, which has an AppArmor bug that blocks
`dockerd` from starting inside an Incus container. The fix shipped in 6.19
(and 6.0.6 LTS) but has not been backported to the Ubuntu archive. WSL2 hosts
are not affected.

Add the Zabbly repo:
```bash
curl -fsSL https://pkgs.zabbly.com/key.asc | sudo gpg --dearmor -o /etc/apt/keyrings/zabbly.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/zabbly.gpg] \
  https://pkgs.zabbly.com/incus/stable $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/zabbly-incus.list
sudo apt update
```

If Ubuntu ESM is enabled it pins distro Incus 6.0.0 at apt priority 510, so
`apt install incus` silently keeps the broken version. Pin the Zabbly version
explicitly across all three packages:
```bash
ZABBLY_VERSION=$(apt-cache madison incus | grep zabbly | head -1 | awk '{print $3}')
sudo apt install incus=$ZABBLY_VERSION incus-base=$ZABBLY_VERSION incus-client=$ZABBLY_VERSION
sudo systemctl restart incus
```

See [docker-in-incus.md](docker-in-incus.md) for the full docker-in-incus
guide including networking paths and other gotchas.

## Docker coexistence (macOS)

Docker Desktop and Incus (Colima) each run in their own hypervisor VM with
isolated network bridges, so `localhost`-based Gitea URLs fail from inside an
Incus container. When passing `GITEA_URL` manually, use Docker Desktop's
internal bridge IP (typically `192.168.64.1`) instead of `localhost`.

## Verifying the installation

Run ALL of these verification commands yourself. All four must succeed.
None require sudo. Report results to the user. If any fail, see
[troubleshooting.md](troubleshooting.md) before asking the user for help.

Check that the Incus client and server are reachable:
```bash
incus version
```

Launch a test container:
```bash
incus launch images:ubuntu/24.04 test-incus
```

Run a command inside it:
```bash
incus exec test-incus -- echo "hello from container"
```

Clean up:
```bash
incus delete test-incus --force
```

## Troubleshooting

For Incus install errors, permission issues, networking problems,
macOS/Colima-specific failures, and more see [troubleshooting.md](troubleshooting.md).
