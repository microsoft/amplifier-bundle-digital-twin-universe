# Known Issues

This document tracks known issues and limitations in the Amplifier Digital Twin Universe (DTU).

## `amplifier-digital-twin update` wipes provider modules

**Status:** Open  
**Severity:** High  
**Workaround:** Available

### Summary

Running `amplifier-digital-twin update <instance>` removes provider modules from a running DTU even when those providers are declared in the profile's `config.providers` section. After update, the DTU is no longer functional — sessions cannot start because no provider can be loaded.

### Reproduction

1. Build a DTU using a profile that declares one or more providers under `config.providers`:
   ```yaml
   config:
     providers:
       - module: provider-anthropic
         config:
           api_key: ${ANTHROPIC_API_KEY}
           default_model: claude-sonnet-4-20250514
   ```

2. Confirm the provider works inside the DTU:
   ```bash
   amplifier-digital-twin exec <instance> -- amplifier tool invoke delegate \
     agent=foundation:explorer model_role=writing instruction="Say hello"
   # Succeeds: real LLM call, response returned, anthropic listed in installed modules
   ```

3. Run `amplifier-digital-twin update <instance>` (no profile change).

4. Re-run the same `tool invoke delegate` command.

### Observed behavior

Step 4 fails with:
```
ModuleNotFoundError: No module named 'amplifier_module_provider_anthropic'
```

Followed by:
```
Module 'provider-anthropic' not found in prepared bundle.
Available modules: ['loop-streaming', 'context-simple', 'tool-todo', 'tool-delegate',
'tool-skills', 'tool-recipes', 'tool-lsp', 'tool-python-check', 'tool-mode', 'tool-mcp',
'tool-apply-patch', 'tool-filesystem', 'tool-bash', 'tool-web', 'tool-search',
'hooks-logging', 'hooks-session-naming', 'hooks-status-context', 'hooks-redaction',
'hooks-todo-reminder', 'hooks-todo-display', 'hooks-streaming-ui', 'hooks-python-check',
'hooks-mode', 'hooks-approval', 'hook-shell', 'hooks-routing']
```

The provider modules declared in `config.providers` are absent from the available-modules list. The bundle modules and hooks declared via `bundle.app` are still present.

### Root cause (hypothesis)

The `update` command runs `amplifier update --yes --force` inside the container. That command re-resolves the active bundle but may not re-install provider modules declared at the `config.providers` level (vs the `bundle.app` level). The update path likely does not mirror the original provision path's full module installation pass.

### Workaround

Destroy and re-launch the DTU instead of running `update`:

```bash
amplifier-digital-twin destroy <instance>
amplifier-digital-twin launch <profile>
```

This is a complete re-provision cycle and works correctly.

---

## Related observations

While building DTUs, the following additional papercuts have been observed (not the primary issue, but may share root causes):

### `bundle.app` heredoc overwrites earlier bundle config

If a profile orders `amplifier bundle add --app <url>` before a heredoc that writes `settings.yaml`, the heredoc silently overwrites the bundle config.

**Workaround:** Inline `bundle.app` into the heredoc rather than relying on prior `bundle add` commands.

### `amplifier-digital-twin check-readiness` schema mismatch

Reports "profile has no readiness checks" even though the profile contains a `readiness:` section. Possibly a schema-version mismatch between the CLI and the running DTU.
