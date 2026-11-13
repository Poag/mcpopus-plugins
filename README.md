<p align="center"><img src="docs/assets/icon.svg" width="96" alt="MCP Octopus icon"></p>

# mcpopus-plugins

Plugins for [MCP Octopus](https://github.com/Poag/mcpopus), the self-hosted MCP host. Each directory under `plugins/` is one plugin,
served at its own path and added to claude.ai as its own connector. This repository is the default plugin source in the host's admin UI.

**Where to go next**

| I want to... | Go to |
| --- | --- |
| Set up a plugin and use it from Claude, step by step | [docs/GUIDE_CLAUDE.md](docs/GUIDE_CLAUDE.md) |
| Run the host itself | [mcpopus: docs/DEPLOY.md](https://github.com/Poag/mcpopus/blob/main/docs/DEPLOY.md) |
| Write my own plugin | [mcpopus: docs/PLUGINS.md](https://github.com/Poag/mcpopus/blob/main/docs/PLUGINS.md) |
| Understand how the Obsidian plugin works | [docs/DESIGN.md](docs/DESIGN.md) |
| Set up the Home Assistant plugin | [docs/DESIGN-homeassistant.md](docs/DESIGN-homeassistant.md) (section 10) |
| Set up the SwarmUI plugin | [docs/DESIGN-swarmui.md](docs/DESIGN-swarmui.md) (section 12) |
| Check what is trusted and what is not | [mcpopus: docs/SECURITY.md](https://github.com/Poag/mcpopus/blob/main/docs/SECURITY.md) |

| Plugin | What it does | State |
| --- | --- | --- |
| [`obsidian`](plugins/obsidian) | Read, search and list an Obsidian vault kept by Self-hosted LiveSync (CouchDB), with end-to-end encryption and obfuscated paths supported, and folders that can be blocked from the model | Read, search, list, tags, and Obsidian Bases (`.base`) files (`list_bases`, `read_base`, and `query_base`, which works out which notes a base would list), and, off by default, folder-limited write, move and delete of notes and `write_base` / `edit_base` for bases (0.6.1; created, never overwritten unless asked; no base delete or move yet) ([design](docs/DESIGN-bases.md)) |
| [`homeassistant`](plugins/homeassistant) | Read the state of a Home Assistant home: entities, areas, history, logbook, with entity and domain policy enforced by the plugin | Read tools, plus write tools (`call_service`, automation trigger, enable and save, `create_helper`) that are off until enabled and allowed by name. Area data needs `wsproto`, which the plugin installs through the host (host 0.3.0) |
| [`swarmui`](plugins/swarmui) | Generate images with a SwarmUI (Stable Diffusion) instance within enforced limits; list models, LoRAs, parameters and history | Read tools, plus generate and cancel (write, off until enabled). Images are not shown to Claude |

Design and specification: [docs/DESIGN.md](docs/DESIGN.md). Catalogue: [docs/PLUGINS.md](docs/PLUGINS.md). Build log: [docs/BUILD_LOG.md](docs/BUILD_LOG.md). Third-party notices: [NOTICE.md](NOTICE.md).

## Install

In the host's admin UI open **Plugins**, press **Fetch** on this repository (it is listed by default), then **Install** the plugin and tick
the trust box. Open the plugin's page to fill in its settings (secrets are stored encrypted), then add
`https://<your host>/<plugin>` as a custom connector in claude.ai. Full walk-through: [docs/GUIDE_CLAUDE.md](docs/GUIDE_CLAUDE.md).
Plugins run inside the host process and are trusted code: read a plugin before you install it.

## Plugins that need Python packages

A plugin can list the packages it needs in `plugin.toml` instead of asking you to add them to the host image:

```toml
min_host_version = "0.3.0"
requirements = ["icalendar==6.1.0"]
```

Entries must be exact pins (`name==version`, optionally with `--hash=sha256:...`). The host (0.3.0 and later) installs them, as wheels only,
when you install the plugin from a source (the install form lists them and the trust box covers them), when you apply an update that changes
them, or from the plugin's page. They live in the host's data volume, never in the image, and a package the host already provides cannot be
replaced. The host needs outbound access to PyPI for this. Rules and limits: the host's
[PLUGINS.md](https://github.com/Poag/mcpopus/blob/main/docs/PLUGINS.md#dependencies) and
[SECURITY.md](https://github.com/Poag/mcpopus/blob/main/docs/SECURITY.md#packages-requested-by-plugins).

## Develop

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Tests load each plugin with the real mcpopus manager and call it with a real MCP client. Interop tests need fixtures written by the
real LiveSync code; CI generates them (`tools/gen_fixtures.mjs`) and they are skipped locally when absent.

## Licence

[MIT](LICENSE). Third-party notices: [NOTICE.md](NOTICE.md).
