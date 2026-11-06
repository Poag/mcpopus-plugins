# Plugin catalogue

One entry per plugin in this repository. The plugin contract (manifest, config, tiers, untrusted output, reload rules) is the host's
[docs/PLUGINS.md](https://github.com/Poag/mcpopus/blob/main/docs/PLUGINS.md); this page is about what these plugins do and how to
set each one up. Install steps are common to all: **Plugins**, **Fetch**, **Install**, tick the trust box, fill in the settings,
**Save and reload**, tick the tools under **Policy**, add `https://<host>/<plugin>` as a connector in claude.ai
([GUIDE_CLAUDE.md](GUIDE_CLAUDE.md)).

| Plugin | Version | Tools (tier) | Design | Set-up |
| --- | --- | --- | --- | --- |
| `obsidian` | see `plugins/obsidian/plugin.toml` | read, search, list, tags, metadata (read); write, edit, move (write); delete (admin) | [DESIGN.md](DESIGN.md) | [GUIDE_CLAUDE.md](GUIDE_CLAUDE.md) |
| `email` | see `plugins/email/plugin.toml` | folders, messages, search, read (all read) | [email.md](email.md) | [GUIDE_EMAIL.md](GUIDE_EMAIL.md) |
| `homeassistant` | see `plugins/homeassistant/plugin.toml` | status, search, get entity, get states, history, logbook, areas, services (all read) | [DESIGN-homeassistant.md](DESIGN-homeassistant.md) | section 10 of the design |

## homeassistant

Read-only view of a Home Assistant home. Entity and domain policy is enforced by the plugin: a hidden entity is absent from every
search, list, count and summary and is answered exactly like a missing one. Defaults hide locks, alarm panels, cameras, people, device
trackers and notifiers. Area and device data need the `wsproto` library in the host image (design doc, section 2); without it
everything else still works. No write tools yet: they follow only after the read-only live check, and are specified in section 9 of
the design. Needs: the Home Assistant address and a long-lived access token from a dedicated non-administrator user.
