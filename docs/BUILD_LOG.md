# Build log (plugins)

Dated entries. "Verified" means the source named was read on the date given. Anything not verified is under "Unverified" and is written
so that it is easy to change. The host's own log is `docs/BUILD_LOG.md` in the mcpopus repository.

## 2026-11-06: homeassistant 0.1.0 (read-only)

### Verified

- Home Assistant developer documentation, read from `home-assistant/developers.home-assistant` on GitHub (`docs/api/rest.md`,
  `docs/api/websocket.md`, `docs/auth_api.md`): REST and WebSocket APIs, bearer token use, long-lived tokens valid for 10 years, the
  WebSocket auth handshake, the REST endpoint list (no areas, devices or entity registry), the template endpoint.
  `developers.home-assistant.io` itself could not be reached from the session's network, so the site was not read.
- Home Assistant core, `home-assistant/core` branch `dev` (version constant 2026.12.0.dev0, so unreleased): `components/api/__init__.py`,
  `components/history/__init__.py`, `components/logbook/rest_api.py`, `components/websocket_api/{auth,commands,const}.py`,
  `components/config/{area,device,entity}_registry.py`, `helpers/*_registry.py`, `components/http/ban.py`. Findings that shaped the
  design: the registry lists are read-only WebSocket commands open to non-administrators; the REST history view applies the user's read
  permissions and requires an entity filter; **the REST logbook view applies none and returns `context_*` fields naming other
  entities**; the services endpoint's shape differs between docs and source; a service call returns every state changed under its
  context; IP banning is off unless a threshold is configured.
- mcpopus host: `requirements.lock` has no WebSocket library. `httpx2` 2.16.0 has a WebSocket client that needs the optional
  `wsproto` package (read from the installed wheel). `wsproto` 1.3.2 is the newest release on PyPI (`pip index versions`).
- The host's plugin API (`PluginContext`, `ctx.stats`, per-plugin policy) is unchanged for this plugin's needs.

### Decisions

- REST for states, history, logbook and services; WebSocket only for the three registry lists. Reason in the design, section 2.
- The area features depend on a library the host image lacks. Not worked round with a hand-written WebSocket client: the plugin uses
  httpx2's own client, reports clearly when `wsproto` is missing, and the smallest host change is written down for Alex.
- The plugin builds its own HTTP client because the host's shared one cannot carry a custom certificate authority or a verify flag.
- A denied entity gets the same answer as a missing one by checking policy before any request, so Home Assistant is never asked about
  a denied entity and cannot be an oracle.
- Calls are paused for 60 seconds after a token refusal, so a revoked token cannot cause repeated failed logins.
- The logbook tool takes one entity, strips `context_*`, and drops entries that name a hidden entity id. It is a heuristic and the
  design says so.
- `docs/PLUGINS.md` and this log live in the plugins repository, because the host repository is read-only for this work.

### Lessons

- Read the implementation, not only the documentation: the logbook's missing permission filter and the services shape are in the
  source, not in the docs.
- A first draft of one "hidden entities are absent" test passed for the wrong reason (the output echoed the ids it was checking
  for). Mutation checks (policy forced open; logbook filters removed) now show the tests fail when the protections go.

### Unverified

Everything against a real Home Assistant; behaviour of the released version (the source read was a development branch); whether a
non-administrator user can be limited to certain entities inside Home Assistant; the behaviour when the recorder or logbook
integrations are off; reverse-proxy WebSocket forwarding. See the design doc, section 3.
