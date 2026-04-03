# Installing Docker Engine

<!-- Agent instruction:
- Run any command you can run yourself (non-sudo). Only hand commands to
  the user when they require sudo or a terminal restart.
- Before starting, briefly tell the user what you're going to install and
  which commands will need their sudo password.
- Present commands to the user one at a time, each in its own code block.
  Terminal copy-paste breaks with multiline blocks.
- Always specify actions like "close and reopen your terminal" when group
  changes or service restarts require it. -->

Docker is required for profiles that use Gitea repos or mock service sidecars.
You should encourage the user to install Docker since many circumstances will require it.
It is **not** required for basic Incus-only profiles.

> **Sources:** Ubuntu install instructions from
> https://docs.docker.com/engine/install/ubuntu/.
> macOS options from https://docs.docker.com/desktop/install/mac-install/
> and https://github.com/abiosoft/colima.
> Post-install group setup from https://docs.docker.com/engine/install/linux-postinstall/.

## Check if Docker is already installed

```bash
docker version
```

If this prints Client and Server versions, Docker is ready. Skip to verification.

## Ubuntu / WSL2

Install prerequisites:
```bash
sudo apt update && sudo apt install -y ca-certificates curl
```

Create the keyrings directory:
```bash
sudo install -m 0755 -d /etc/apt/keyrings
```

Download Docker's GPG key:
```bash
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
```

Make the key readable:
```bash
sudo chmod a+r /etc/apt/keyrings/docker.asc
```

Add the Docker apt repository:
```bash
echo "Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")\nComponents: stable\nArchitectures: $(dpkg --print-architecture)\nSigned-By: /etc/apt/keyrings/docker.asc" | sudo tee /etc/apt/sources.list.d/docker.sources
```

Install Docker:
```bash
sudo apt update && sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Add your user to the `docker` group so you can run without sudo:
```bash
sudo usermod -aG docker $USER
```

Apply the group change (or close and reopen your terminal):
```bash
newgrp docker
```

## macOS

Install [Docker Desktop](https://docs.docker.com/desktop/install/mac-install/) or use Colima.

Install the Docker CLI and Colima:
```bash
brew install docker colima
```

Start Colima:
```bash
colima start
```

## Verifying the installation

```bash
docker run hello-world
```

This should pull a test image and print a confirmation message. If it fails with a permission error, the `newgrp docker` step was missed or you need to close and reopen your terminal.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `docker: command not found` | Not installed -- run the install steps above |
| `permission denied` on `docker run` | Run `sudo usermod -aG docker $USER`, then close and reopen your terminal |
| `Cannot connect to the Docker daemon` | Docker service not running -- `sudo systemctl start docker` |
| macOS: Docker commands fail | Start Docker Desktop or run `colima start` |
