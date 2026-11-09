# Plugin catalogue

One entry per plugin in this repository. The plugin contract (manifest, config, tiers, untrusted output, reload rules) is the host's
[docs/PLUGINS.md](https://github.com/Poag/mcpopus/blob/main/docs/PLUGINS.md); this page is about what these plugins do and how to
set each one up. Install steps are common to all: **Plugins**, **Fetch**, **Install**, tick the trust box, fill in the settings,
**Save and reload**, tick the tools under **Policy**, add `https://<host>/<plugin>` as a connector in claude.ai
([GUIDE_CLAUDE.md](GUIDE_CLAUDE.md)).

| Plugin | Version | Tools (tier) | Design | Set-up |
| --- | --- | --- | --- | --- |
| `obsidian` | see `plugins/obsidian/plugin.toml` | read, search, list, tags, metadata, list_bases, read_base (read); write, edit, move (write); delete (admin) | [DESIGN.md](DESIGN.md), [DESIGN-bases.md](DESIGN-bases.md) | [GUIDE_CLAUDE.md](GUIDE_CLAUDE.md) |
| `email` | see `plugins/email/plugin.toml` | folders, messages, search, read (all read) | [email.md](email.md) | [GUIDE_EMAIL.md](GUIDE_EMAIL.md) |
| `homeassistant` | see `plugins/homeassistant/plugin.toml` | status, search, get entity, get states, history, logbook, areas, services (read); call_service, trigger_automation, set_automation_enabled, save_automation (write); delete_automation (admin); list_automations, get_automation_definition (read) | [DESIGN-homeassistant.md](DESIGN-homeassistant.md) | section 10 of the design |
| `swarmui` | see `plugins/swarmui/plugin.toml` | status, models, LoRAs, describe, parameters, images, job status (read); generate, cancel (write) | [DESIGN-swarmui.md](DESIGN-swarmui.md) | section 12 of the design |

## obsidian: Bases

From 0.4.0 the plugin lists and reads Obsidian Bases (`.base` files): `list_bases` and `read_base` (read, untrusted output). `read_base` returns the raw
YAML, a summary of its filters, formulas and views, and the notes that embed it; nothing in a base is evaluated. Bases follow the folder policy
exactly like notes (a blocked base is answered like a missing one), `get_note_metadata` says whether a linked `.base` is found, and `vault_status` gains
counts by file extension and for bases. The parsed summary needs PyYAML in the host image; without it the raw text is still returned
([DESIGN-bases.md](DESIGN-bases.md), sections 7 and 8). Querying a base and writing bases are planned and not built.

## homeassistant

Read-only view of a Home Assistant home. Entity and domain policy is enforced by the plugin: a hidden entity is absent from every
search, list, count and summary and is answered exactly like a missing one. Defaults hide locks, alarm panels, cameras, people, device
trackers and notifiers. Area and device data need the `wsproto` library in the host image (design doc, section 2); without it
everything else still works. `call_service` (write, 0.2.0) calls one allowed service on one named, allowed entity and returns the state before and after; it is off until
the owner ticks it, and it needs `allowed_services` and `write_entity_allowlist` as well (design, section 9). Automations can be listed, and triggered, enabled and disabled when they are in a Home Assistant category the owner names (section 9b); definitions can be read, created, replaced and deleted for automations the owner's prefix marks as Claude's, when the token's user is an administrator (recommended: a dedicated user marked local access only) and within a strict subset (section 9c). Needs: the Home Assistant address and a long-lived access token from a dedicated non-administrator user.

## swarmui

Generate images with a SwarmUI (Stable Diffusion) instance, within limits the plugin enforces: no `<...>` prompt commands unless the
owner allows them, models and LoRAs only from the allow and deny lists, size, count, step, rate, queue and concurrency limits, extra
settings only if the owner lists them. Use a dedicated SwarmUI user with a limited role and its own token; `status` warns if the account
can do more than the connector needs. Generation and cancel are write tools, off until ticked by name. Claude cannot see the images (the
host returns text only): results give paths and links. Live progress needs `wsproto` in the host image; without it generation still works.

