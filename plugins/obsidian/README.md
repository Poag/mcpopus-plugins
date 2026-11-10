# obsidian

Read, search and list an Obsidian vault kept by Self-hosted LiveSync (CouchDB), with end-to-end encryption and obfuscated paths supported,
folders that can be blocked from the model, and folder-limited write, move and delete tools (off by default).

- Set-up guide: [docs/GUIDE_CLAUDE.md](../../docs/GUIDE_CLAUDE.md)
- How it works: [docs/DESIGN.md](../../docs/DESIGN.md)
- Settings are in `plugin.toml` and the `Config` class in `__init__.py`; secrets are stored encrypted by the host.

## Packages

`plugin.toml` lists `PyYAML==6.0.3` (hashed, wheels only, for the Python 3.13 image on amd64 or arm64) under `requirements`, and
`min_host_version = "0.3.0"`. The host installs it when you add or update the plugin (after you tick the trust box) or from the plugin's page. It
parses Bases (`.base`) files for the summary in `read_base`, using the safe loader only. If the import still fails, `read_base` returns the raw text
with "PyYAML is missing: install the plugin's packages from its page". If the host moves to another Python version, the two hashes need replacing.
