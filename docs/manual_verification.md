# Manual Verification

This walkthrough mirrors the same flow used by
`tests/test_e2e_amplifier_user_sim.py`:

- start Gitea
- mirror `amplifier-core` and `amplifier-module-provider-anthropic`
- push the same sentinel changes the test applies
- launch `amplifier-user-sim`
- verify both overrides inside the environment
- clean everything up

## Prerequisites

- Incus running
- Docker running
- `amplifier-gitea` on `PATH`
- GitHub token available through `GH_TOKEN`, `GITHUB_TOKEN`, or `gh auth token`
- `ANTHROPIC_API_KEY` exported

The commands below assume you are starting from the
`amplifier-bundle-digital-twin-universe` repo root.

## 1. Start Gitea

```bash
cd /path/to/amplifier-bundle-digital-twin-universe

GITHUB_TOKEN=${GH_TOKEN:-${GITHUB_TOKEN:-$(gh auth token)}}

GITEA_JSON=$(amplifier-gitea create --port 10110)
printf '%s\n' "$GITEA_JSON"

GITEA_ID=$(python -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"$GITEA_JSON")
GITEA_TOKEN=$(python -c 'import json,sys; print(json.load(sys.stdin)["token"])' <<<"$GITEA_JSON")
GITEA_URL=$(python -c 'import json,sys; print(json.load(sys.stdin)["gitea_url"])' <<<"$GITEA_JSON")
```

## 2. Mirror Both Repos From GitHub

```bash
amplifier-gitea mirror-from-github "$GITEA_ID" \
  --github-repo https://github.com/microsoft/amplifier-core \
  --github-token "$GITHUB_TOKEN"

amplifier-gitea mirror-from-github "$GITEA_ID" \
  --github-repo https://github.com/microsoft/amplifier-module-provider-anthropic \
  --github-token "$GITHUB_TOKEN"
```

## 3. Push The Same amplifier-core Change As The Test

The full end-to-end test sets all relevant `amplifier-core` versions to
`99.0.0`.

```bash
rm -rf /tmp/amplifier-core-manual
git clone \
  "http://admin:${GITEA_TOKEN}@localhost:10110/admin/amplifier-core.git" \
  /tmp/amplifier-core-manual

python - <<'PY'
from pathlib import Path

repo = Path("/tmp/amplifier-core-manual")
replacements = [
    (repo / "pyproject.toml", 'version = "1.3.3"', 'version = "99.0.0"'),
    (repo / "bindings/python/Cargo.toml", 'version = "1.3.3"', 'version = "99.0.0"'),
    (repo / "crates/amplifier-core/Cargo.toml", 'version = "1.3.3"', 'version = "99.0.0"'),
    (repo / "python/amplifier_core/__init__.py", '__version__ = "1.0.7"', '__version__ = "99.0.0"'),
]

for path, old, new in replacements:
    text = path.read_text()
    if old not in text:
        raise SystemExit(f"expected {old!r} in {path}")
    path.write_text(text.replace(old, new, 1))
PY

git -C /tmp/amplifier-core-manual add -A
git -C /tmp/amplifier-core-manual \
  -c user.name='Amplifier Digital Twin Universe Tests' \
  -c user.email='digital-twin-universe-tests@example.com' \
  commit -m 'test: override amplifier-core version'

git -C /tmp/amplifier-core-manual remote set-url origin \
  "http://admin:${GITEA_TOKEN}@localhost:10110/admin/amplifier-core.git"

git -C /tmp/amplifier-core-manual push origin HEAD:main --force
```

## 4. Push The Same Provider Marker As The Test

The full end-to-end test injects a warning marker into
`amplifier-module-provider-anthropic` just before the `api_key` check.

```bash
rm -rf /tmp/amplifier-module-provider-anthropic-manual
git clone \
  "http://admin:${GITEA_TOKEN}@localhost:10110/admin/amplifier-module-provider-anthropic.git" \
  /tmp/amplifier-module-provider-anthropic-manual

python - <<'PY'
from pathlib import Path

path = Path(
    "/tmp/amplifier-module-provider-anthropic-manual/"
    "amplifier_module_provider_anthropic/__init__.py"
)
old = "    if not api_key:\\n"
new = (
    '    logger.warning("AMPLIFIER_PROVIDER_ANTHROPIC_TEST_MARKER")\\n'
    "\\n"
    "    if not api_key:\\n"
)

text = path.read_text()
if old not in text:
    raise SystemExit(f"expected {old!r} in {path}")
path.write_text(text.replace(old, new, 1))
PY

git -C /tmp/amplifier-module-provider-anthropic-manual add -A
git -C /tmp/amplifier-module-provider-anthropic-manual \
  -c user.name='Amplifier Digital Twin Universe Tests' \
  -c user.email='digital-twin-universe-tests@example.com' \
  commit -m 'test: inject provider warning marker'

git -C /tmp/amplifier-module-provider-anthropic-manual remote set-url origin \
  "http://admin:${GITEA_TOKEN}@localhost:10110/admin/amplifier-module-provider-anthropic.git"

git -C /tmp/amplifier-module-provider-anthropic-manual push origin HEAD:main --force
```

## 5. Launch amplifier-user-sim

```bash
DIGITAL_TWIN_JSON=$(uv run amplifier-digital-twin launch amplifier-user-sim \
  --var "GITEA_URL=${GITEA_URL}" \
  --var "GITEA_TOKEN=${GITEA_TOKEN}")

printf '%s\n' "$DIGITAL_TWIN_JSON"

DIGITAL_TWIN_ID=$(python -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"$DIGITAL_TWIN_JSON")
```

## 6. Get Into The Environment

```bash
uv run amplifier-digital-twin exec "$DIGITAL_TWIN_ID"
```

Or run one command at a time:

```bash
uv run amplifier-digital-twin exec "$DIGITAL_TWIN_ID" -- amplifier --version
```

## 7. Verify The Same Signals As The Test

First verify the installed Amplifier tool environment is using the overridden
`amplifier-core` wheel:

```bash
uv run amplifier-digital-twin exec "$DIGITAL_TWIN_ID" -- bash -lc '
  TOOL_PYTHON=$(find /root/.local/share/uv/tools/amplifier -path "*/bin/python3" | head -1)
  "$TOOL_PYTHON" -c "import amplifier_core._engine as engine; print(engine.__version__)"
'
```

Expected output:

```text
99.0.0
```

Then verify the rewritten provider is loaded during an Amplifier run:

```bash
uv run amplifier-digital-twin exec "$DIGITAL_TWIN_ID" -- bash -lc "
  cd /home/user/project
  amplifier run 'respond with exactly: HELLO_DTU_PROVIDER'
"
```

Expected signals:

- the output contains `HELLO_DTU_PROVIDER`
- the CLI output also shows `AMPLIFIER_PROVIDER_ANTHROPIC_TEST_MARKER`

## 8. Clean Up

```bash
# Type "exit" to leave the Digital Twin environment.
uv run amplifier-digital-twin destroy "$DIGITAL_TWIN_ID"
amplifier-gitea destroy "$GITEA_ID"
rm -rf /tmp/amplifier-core-manual
rm -rf /tmp/amplifier-module-provider-anthropic-manual
```
