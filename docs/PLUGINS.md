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
| `homeassistant` | see `plugins/homeassistant/plugin.toml` | status, search, get entity, get states, history, logbook, areas, services (read); call_service, trigger_automation, set_automation_enabled (write); list_automations (read) | [DESIGN-homeassistant.md](DESIGN-homeassistant.md) | section 10 of the design |

## homeassistant

Read-only view of a Home Assistant home. Entity and domain policy is enforced by the plugin: a hidden entity is absent from every
search, list, count and summary and is answered exactly like a missing one. Defaults hide locks, alarm panels, cameras, people, device
trackers and notifiers. Area and device data need the `wsproto` library in the host image (design doc, section 2); without it
everything else still works. `call_service` (write, 0.2.0) calls one allowed service on one named, allowed entity and returns the state before and after; it is off until
the owner ticks it, and it needs `allowed_services` and `write_entity_allowlist` as well (design, section 9). Automations can be listed, and triggered, enabled and disabled when they are in a Home Assistant category the owner names (section 9b); reading and editing definitions is designed but not built (section 9c). Needs: the Home Assistant address and a long-lived access token from a dedicated non-administrator user.
