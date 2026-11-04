# obsidian

Read, search and list an Obsidian vault kept by Self-hosted LiveSync (CouchDB), with end-to-end encryption and obfuscated paths supported,
folders that can be blocked from the model, and folder-limited write, move and delete tools (off by default).

- Set-up guide: [docs/GUIDE_CLAUDE.md](../../docs/GUIDE_CLAUDE.md)
- How it works: [docs/DESIGN.md](../../docs/DESIGN.md)
- Settings are in `plugin.toml` and the `Config` class in `__init__.py`; secrets are stored encrypted by the host.
