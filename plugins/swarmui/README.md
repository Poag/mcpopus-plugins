# swarmui

Generate images with a SwarmUI (Stable Diffusion) instance and look at its models, parameters and history. Generation is limited by the
plugin: no `<...>` prompt commands by default, model and LoRA lists, size, count, step, rate, queue and concurrency limits, and no image
inputs or output locations.

- Design, verified facts, safety rules and the live checklist: [docs/DESIGN-swarmui.md](../../docs/DESIGN-swarmui.md)
- Settings are the `Config` class in `__init__.py`; the auth token is stored encrypted by the host.

Make a dedicated SwarmUI user whose role holds only generation, model, history and status permissions, and use its token. Generation and
cancel are write tools: tick them under Policy to turn them on. Claude cannot see the generated images; results give paths and links.

## Packages

`plugin.toml` lists `wsproto==1.3.2` and `h11==0.16.0` (hashed, wheels only) under `requirements`, and `min_host_version = "0.3.0"`. The host installs
them when you add or update the plugin (after you tick the trust box) or from the plugin's page. They give live progress over SwarmUI's WebSocket
(httpx2's WebSocket client imports `wsproto`). If they are missing the plugin does not load; if the import still fails, `status` says live
progress is off and why, and generation still works.
