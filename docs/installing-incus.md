# Installing Incus

<!-- Agent instruction:
- Run any command you can run yourself (non-sudo). Only hand commands to
  the user when they require sudo or a terminal restart.
- Before starting, briefly tell the user what you're going to install and
  which commands will need their sudo password.
- Present commands to the user one at a time, each in its own code block.
  Terminal copy-paste breaks with multiline blocks.
- Always specify actions like "close and reopen your terminal" when group
  changes or service restarts require it. -->

Incus is the container runtime used by the Digital Twin Universe.

> **Sources:** Instructions derived from the official Incus install docs at
> https://linuxcontainers.org/incus/docs/main/installing/ and the Colima project
> at https://github.com/abiosoft/colima. Docker coexistence fix from
> https://linuxcontainers.org/incus/docs/main/howto/network_bridge_firewalld/.

## Ubuntu / WSL2 (Ubuntu 24.04+)

Incus is in the default Ubuntu repos.

Install:
```bash
sudo apt update && sudo apt install -y incus
```

Add your user to the `incus-admin` group:
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

The Incus server is Linux-only. On macOS, use Colima to run it inside a lightweight VM.

Install both the Incus client and Colima:
```bash
brew install incus colima
```

Start Colima with Incus as the runtime:
```bash
colima start --runtime incus
```

After `colima start`, the `incus` CLI automatically connects to the Colima VM. No additional configuration needed.

### Prerequisites: Docker (optional)

Docker is only needed for profiles that use Gitea repos or mock service sidecars. If the profile you're launching requires Docker, see [installing-docker.md](installing-docker.md).

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

## Verifying the installation

Run ALL of these verification commands yourself. All four must succeed.
None require sudo. Report results to the user. If any fail, diagnose
using the troubleshooting table before asking the user for help.

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

If `incus version` shows `Server version: unreachable`, the shell doesn't have the `incus-admin` group yet. The user needs to run `newgrp incus-admin` or close and reopen their terminal.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `Server version: unreachable` | `newgrp incus-admin` or close and reopen your terminal |
| Container launches but no network | Docker coexistence issue -- apply the fix above |
| `incus: command not found` | Package not installed -- run the install steps for the platform |
| `Error: not found` on `incus launch` | `sudo incus admin init --minimal` was not run |
| macOS: `incus` works but no server | `colima start --runtime incus` not running |
