# swarmui

Generate images with a SwarmUI (Stable Diffusion) instance and look at its models, parameters and history. Generation is limited by the
plugin: no `<...>` prompt commands by default, model and LoRA lists, size, count, step, rate, queue and concurrency limits, and no image
inputs or output locations.

- Design, verified facts, safety rules and the live checklist: [docs/DESIGN-swarmui.md](../../docs/DESIGN-swarmui.md)
- Settings are the `Config` class in `__init__.py`; the auth token is stored encrypted by the host.

Make a dedicated SwarmUI user whose role holds only generation, model, history and status permissions, and use its token. Generation and
cancel are write tools: tick them under Policy to turn them on. Claude cannot see the generated images; results give paths and links.
Live progress needs the wsproto library in the host image; without it everything still works.
