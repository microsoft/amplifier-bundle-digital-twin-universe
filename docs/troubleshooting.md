# Troubleshooting

Reference for issues with Incus, Docker, and Digital Twin Universe
environments. Organized by category. Install-step-specific tables also live in
[installing-incus.md](installing-incus.md) and [installing-docker.md](installing-docker.md).

## Categories

- [Version mismatches](#version-mismatches)
- [Incus permissions and access](#incus-permissions-and-access)
- [Incus install issues](#incus-install-issues)
- [Docker install issues](#docker-install-issues)
- [Docker + Incus coexistence](#docker--incus-coexistence)
- [Docker inside Incus (nesting)](#docker-inside-incus-nesting)
- [Reusable / published images](#reusable--published-images)
- [DTU launch and provisioning](#dtu-launch-and-provisioning)
- [DTU CLI usage](#dtu-cli-usage)


## Version mismatches

Before deeper troubleshooting, confirm CLI, bundle, and profiles are aligned.

Check the CLI version and compare against the latest
[tag](https://github.com/microsoft/amplifier-bundle-digital-twin-universe/tags):

```bash
amplifier-digital-twin --version
uv tool upgrade amplifier-bundle-digital-twin-universe
```

Refresh the Amplifier bundle cache so it lines up with the CLI:

```bash
amplifier reset --remove cache -y
```

Check your saved profiles against the current schema in
[profiles.md](profiles.md). Symptoms of drift: `UnknownProfileFieldWarning`,
`requires exactly one of ...` validation errors, or `Invalid match_mode`.

To review what changed and update your profiles, clone the repo to a
temporary directory and read the latest `docs/` and `skills/`:

```bash
git clone --depth 1 https://github.com/microsoft/amplifier-bundle-digital-twin-universe \
  /tmp/dtu-latest
less /tmp/dtu-latest/docs/profiles.md
less /tmp/dtu-latest/skills/digital-twin-universe/SKILL.md
```

If updating is disruptive, downgrade the CLI to match your profiles:

```bash
uv tool install --reinstall \
  'git+https://github.com/microsoft/amplifier-bundle-digital-twin-universe@v<tag>'
```


## Incus permissions and access

### `Server version: unreachable` from `incus version`

Your shell does not have the `incus-admin` group. Apply group membership:
```bash
sudo usermod -aG incus-admin $USER
newgrp incus-admin
```

`newgrp` only affects the current shell — any process spawned before the group
change (tmux sessions, Amplifier sessions, background services) still lacks the
group. Log out and back in (or reboot) for a full propagation.

### `You don't have the needed permissions to talk to the incus daemon`

Same fix as above — user is not in `incus-admin`.


## Incus install issues

### `incus: command not found`

Package not installed. See [installing-incus.md](installing-incus.md) for the
platform install path.

### `Error: not found` on `incus launch`

`sudo incus admin init --minimal` was not run after install. Run it once.

### macOS: `sudo: incus: command not found` during `colima start`

Incus is not installed inside the Colima VM. Colima 0.10.x registers the
`--runtime incus` flag but does not install the binary in the Lima/Ubuntu VM.
Run [installing-incus.md macOS step 3](installing-incus.md#3-install-incus-inside-the-colima-vm).

### macOS: `Invalid config: No uid/gid allocation configured` on `incus launch`

`/etc/subuid` and `/etc/subgid` lack a `root:` entry inside the Colima VM.
`incus admin init --minimal` does not populate these. Run
[installing-incus.md macOS step 4](installing-incus.md#4-allocate-uidgid-for-root-inside-the-vm).

### macOS: `This client hasn't been configured to use a remote server yet`

Host's `local` Incus remote points at `/var/lib/incus/unix.socket`, which does
not exist on macOS. Configure a remote pointing at the Colima-forwarded socket.
See [installing-incus.md macOS step 6](installing-incus.md#6-configure-the-host-incus-client-to-use-the-colima-socket).

### macOS: `incus` works but no server

`colima start --runtime incus` is not running. Start Colima.


## Docker install issues

### `docker: command not found`

Not installed. See [installing-docker.md](installing-docker.md).

### `permission denied` on `docker run`

User is not in the `docker` group:
```bash
sudo usermod -aG docker $USER
newgrp docker
```

Close and reopen your terminal if `newgrp` does not take effect for spawned
processes.

### `Cannot connect to the Docker daemon`

Docker service not running:
```bash
sudo systemctl start docker
```

### macOS: Docker commands fail

Start Docker Desktop or run `colima start`.


## Docker + Incus coexistence

### Containers launch but have no internet (Linux / WSL2)

Docker sets the kernel's iptables `FORWARD` chain to `DROP`, blocking Incus
bridge traffic.

**Symptoms:** `apt-get update` fails inside containers; containers cannot reach
external hosts.

**Fix (one-time):**
```bash
echo '{"ip-forward-no-drop": true}' | sudo tee /etc/docker/daemon.json
sudo systemctl restart docker
```

On WSL2, also restart WSL from PowerShell:
```powershell
wsl --shutdown
```

Then reopen your WSL terminal.

### Containers launch but `apt-get update` fails inside (macOS / Colima)

Same FORWARD-DROP issue, but inside the Colima VM (which has Docker
pre-installed even when `--runtime incus` is selected). The Colima VM itself
can reach the internet, but Incus containers inside it cannot.

Apply the fix inside the VM:
```bash
colima ssh
echo '{"ip-forward-no-drop": true}' | sudo tee /etc/docker/daemon.json
sudo systemctl restart docker
exit
```

### Bare-metal Ubuntu: networking still fails after `daemon.json` fix

The `daemon.json` fix may not be sufficient on bare-metal. Add explicit iptables
rules for the Incus bridge:
```bash
sudo iptables -I DOCKER-USER -i incusbr0 -j ACCEPT
sudo iptables -I DOCKER-USER -o incusbr0 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
```

Persist across reboots via `iptables-persistent` or `/etc/rc.local`.

### macOS: Gitea (or other host-Docker service) unreachable from Incus container

**Symptom:** `amplifier-digital-twin launch` hangs or fails with errors like:

```
fatal: unable to access 'http://<ip>:10110/.../': Could not connect to server
```

when the container tries to clone from a Gitea instance running in Docker.

**Cause:** On macOS, Incus runs inside a Colima (or similar) VM and Docker Desktop runs inside its own hypervisor VM. The two VMs have isolated network bridges and cannot reach each other directly. None of the obvious addresses route correctly:

| Address | Why it fails |
|---|---|
| `localhost` / `127.0.0.1` | Resolves to the container itself |
| Your LAN IP (e.g. `10.0.0.42`) | Works briefly, breaks whenever you change WiFi networks |
| `192.168.100.1` (Incus VM gateway) | Docker Desktop is not on that network |

**Fix:** Use Docker Desktop's internal VM bridge IP `192.168.64.1` — it is stable across WiFi changes and reachable from inside the Incus container:

```bash
amplifier-digital-twin launch <profile> --var GITEA_URL=http://192.168.64.1:<port>
```

Verify reachability first from inside a test container:

```bash
incus exec <container> -- curl -sf http://192.168.64.1:10110/
```

**Note:** The `dtu-profile-builder` agent probes connectivity from a test container and discovers the correct IP automatically. This issue only surfaces when launching `amplifier-digital-twin` manually with a hardcoded `GITEA_URL`, or when the agent skips its probe step.


## Docker inside Incus (nesting)

> **If a profile runs Docker inside Incus** (any provisioning that installs
> Docker), read [docker-in-incus.md](docker-in-incus.md) in full. It covers
> networking paths, AppArmor, and `security.nesting` requirements end-to-end.
> `security.nesting=true` is applied by default to every DTU launch; profiles
> only need to mention it when opting out.

### `dockerd` fails to start inside the Incus container with AppArmor errors

Affects Incus < 6.0.6 LTS / < 6.19 with `runc >= 1.2.8`. The fix shipped in
Incus 6.19 (backported to 6.0.6 LTS) but distro Ubuntu 24.04 still ships 6.0.0.
WSL2 hosts are not affected.

**Symptoms:** AppArmor permission denied errors on `/proc/sys/` or `/sys/`.

**Fix:** Install Incus from the Zabbly repo. See
[installing-incus.md → Docker coexistence → Bare-metal Ubuntu running Docker inside Incus](installing-incus.md#bare-metal-ubuntu-running-docker-inside-incus-install-from-zabbly).
Note that Ubuntu ESM pins distro Incus 6.0.0 at apt priority 510, so a plain
`apt install incus` silently keeps the broken version — pin all three
packages to the Zabbly version explicitly.

### `dockerd` cannot create namespaces / cgroups

`security.nesting=true` is applied by default to every DTU launch, so this
should not normally trip. It only happens if a profile explicitly opts out by
setting `security.nesting: "false"` in `base.config`. Remove that override
to fall back to the default, or set the setting globally on the host's Incus
profile (applies to all Incus containers):

```bash
incus profile set default security.nesting=true
```


## Reusable / published images

Baking a provisioned container into a reusable image with `incus publish` and
launching later containers from it (a profile whose `base.image` is a `local:`
image you published).

### Launching from a published image fails with `tar` hard link errors

```
Unpack failed: ... tar ... Cannot hard link to '.../dev/...': No such file or directory
```

Incus < 6.0.1 unpacks with an unanchored `tar --exclude=dev/*`, which skips every
`dev`-named directory at any depth (common in a Docker store or `node_modules`),
breaking hardlinks whose target lives under one. It also silently drops those
files even when it does not error ([lxc/incus#815](https://github.com/lxc/incus/issues/815)).

**Fix:** Upgrade Incus to 6.0.1+ (Incus 7 LTS includes it); see
[installing-incus.md → Recommended version](installing-incus.md#recommended-version).
The published image is intact, so no re-publish is needed.

### Docker images are missing after launching from a published image

`docker images` is empty inside a container launched from an image baked with
Docker images present (and `docker run` of them fails). Docker only sees the
storage backend it is currently configured for; toggling
`features.containerd-snapshotter` in `/etc/docker/daemon.json` between bake and
reuse hides the images (still listed by `ctr -n moby images ls`).

**Fix:** Keep Docker's storage backend identical between bake and reuse.


## DTU launch and provisioning

### `launch` hangs on provisioning

Usually a networking issue. Fix Docker/Incus networking first
([Docker + Incus coexistence](#docker--incus-coexistence)), then retry. Check
container state with `incus list`.

### `apt-get update` fails with `archive.ubuntu.com` errors during provisioning

Errors include `Failed to fetch`, `Mirror sync in progress?`, or exit 124
timeout with `Ign:` lines for `archive.ubuntu.com`. This is an upstream outage,
not a `url_rewrites`/mitmproxy bug — do not investigate the proxy.

**Workaround:** switch the profile's `base.image` to a Debian image (e.g.
`images:debian/12`) and retry. Debian's mirrors are independent of the Ubuntu
archive. Be aware that a base image switch may have other downstream effects.

### Provisioning fails with `command not found`

The provisioned tool is not installed yet at that stage. Check `setup_cmds`
ordering — install runtime/package manager before commands that depend on it.

### Amplifier inside container is extremely slow / hangs on `Loading foundation`

Check container networking (the foundation install fetches from GitHub) and
compute allocation. If networking is the cause, work through
[Docker + Incus coexistence](#docker--incus-coexistence) first.

### Streaming from an in-container service arrives all at once

A client inside the environment sees nothing until the response finishes, then
gets the whole body at once. The proxy buffers response bodies, so loopback
traffic must not go through it. Launch exempts loopback since v0.3.2; check
nothing is overriding it:

```bash
amplifier-digital-twin exec <id> -- bash -lc 'env | grep -i proxy'
# expect: no_proxy=localhost,127.0.0.1,::1 (and NO_PROXY)
```

### `Environment not found` for a previously created environment

The Incus container was stopped or removed externally. Create a fresh
environment.


## DTU CLI usage

### `Got unexpected extra arguments` when passing `--var`

JSON output from a subshell expansion is being splatted as multiple arguments.
Extract just the value:
```bash
# Wrong
--var GITEA_TOKEN=$(amplifier-gitea token <id>)

# Right
--var GITEA_TOKEN=$(amplifier-gitea token <id> | jq -r .token)
```


## Not listed here?

If the behavior contradicts these docs, check whether it changed between
versions: [CHANGELOG.md](CHANGELOG.md).
