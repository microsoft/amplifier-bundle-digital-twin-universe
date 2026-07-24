# Changelog

## 0.3.2

- **Loopback is exempted from the environment proxy.** When a profile enables
  the proxy (`url_rewrites` or `mock_services`), `/etc/profile.d/dtu-env.sh`
  now also exports `no_proxy` / `NO_PROXY` = `localhost,127.0.0.1,::1`.
  Previously an in-container client talking to an in-container server on
  localhost was routed through mitmproxy, which buffers whole response bodies:
  SSE and token streaming inside an environment arrived all at once at the end.
  Rewriting for real hosts and the `pypi_overrides` redirect are unaffected,
  both happen proxy-side. A profile can still override the default by
  forwarding the host's own `no_proxy` via `passthrough`.
