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
| Set up the Gmail (IMAP) plugin | [docs/GUIDE_EMAIL.md](docs/GUIDE_EMAIL.md) |
| Check what is trusted and what is not | [mcpopus: docs/SECURITY.md](https://github.com/Poag/mcpopus/blob/main/docs/SECURITY.md) |

| Plugin | What it does | State |
| --- | --- | --- |
| [`obsidian`](plugins/obsidian) | Read, search and list an Obsidian vault kept by Self-hosted LiveSync (CouchDB), with end-to-end encryption and obfuscated paths supported, and folders that can be blocked from the model | Read, search, list, tags; folder-limited write, move and delete (off by default) |
| [`email`](plugins/email) | Read a mailbox over IMAP, built for Gmail with an app password: list, search and read messages | Read-only: nothing is marked read, moved or deleted. No write tools |

Design and specification: [docs/DESIGN.md](docs/DESIGN.md) and [docs/email.md](docs/email.md). Gmail set-up: [docs/GUIDE_EMAIL.md](docs/GUIDE_EMAIL.md). Third-party notices: [NOTICE.md](NOTICE.md).

## Install

In the host's admin UI open **Plugins**, press **Fetch** on this repository (it is listed by default), then **Install** the plugin and tick
the trust box. Open the plugin's page to fill in its settings (secrets are stored encrypted), then add
`https://<your host>/<plugin>` as a custom connector in claude.ai. Full walk-through: [docs/GUIDE_CLAUDE.md](docs/GUIDE_CLAUDE.md).
Plugins run inside the host process and are trusted code: read a plugin before you install it.

## Develop

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

Tests load each plugin with the real mcpopus manager and call it with a real MCP client. Interop tests need fixtures written by the
real LiveSync code; CI generates them (`tools/gen_fixtures.mjs`) and they are skipped locally when absent.

## Licence

[MIT](LICENSE). Third-party notices: [NOTICE.md](NOTICE.md).
