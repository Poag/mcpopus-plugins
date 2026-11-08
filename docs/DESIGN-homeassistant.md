# homeassistant: design

Written 6 November 2026. Specification of the `homeassistant` plugin (endpoint `/homeassistant`), version 0.3.0. Reads, plus write tools (`call_service`, `trigger_automation`, `set_automation_enabled`) that are off until the owner enables each by name and names what they may touch. Conventions
and the rules every plugin follows are in [DESIGN.md](DESIGN.md) section 1; the host's plugin contract is in the mcpopus repository
(`docs/PLUGINS.md`, `docs/SECURITY.md`). Status: the read tools passed a live check on 7 November 2026 (section 11). `call_service` (0.2.0) and the automation tools (0.3.0) are
built and tested against a fake Home Assistant only; their live checks are sections 10b and 10c.

## 1. Goal

Let Claude read the state of Alex's home: which entities exist, what state they are in, which area they are in, what they did recently.
Version 0.2.0 adds a small, explicitly allowed set of changes (section 9). Policy about what Claude may see is enforced by the plugin,
server side, and a hidden entity is indistinguishable from a missing one.

## 2. What was verified

All read on 6 November 2026. "Docs" is the official developer documentation repository, `home-assistant/developers.home-assistant` on
GitHub (the site developers.home-assistant.io was not reachable from the build session, so its source files were read instead). "Core" is
`home-assistant/core` on its `dev` branch, where `homeassistant/const.py` said version **2026.12.0.dev0**: that is an unreleased
development branch, so details may differ from the release Alex runs (unverified, section 3).

| Fact | Source |
| --- | --- |
| Remote access is a REST API on the web port (8123, or 80 on Home Assistant OS) and a WebSocket API at `/api/websocket` | Docs `docs/api/rest.md`, `docs/api/websocket.md` |
| Every REST call needs `Authorization: Bearer <token>`; a token is a "Long-Lived Access Token" made on a user's profile page | Docs `docs/api/rest.md` |
| Long-lived access tokens are valid for 10 years; they can be revoked on the profile page. Short-lived tokens last 30 minutes (`expires_in: 1800`) and need the OAuth refresh flow, which this plugin does not use | Docs `docs/auth_api.md` |
| WebSocket: server sends `auth_required`, client sends `{"type":"auth","access_token":...}`, server answers `auth_ok` or `auth_invalid` and disconnects | Docs `docs/api/websocket.md`; Core `components/websocket_api/auth.py` |
| REST has no endpoint for areas, devices or the entity registry. Areas are reachable by WebSocket (`config/area_registry/list`, `config/device_registry/list`, `config/entity_registry/list`) or by the template endpoint, which the brief forbids | Docs `docs/api/rest.md` (full endpoint list); Core `components/config/{area,device,entity}_registry.py` |
| The three registry list commands carry no `require_admin`, so a non-administrator token can run them. Mutating registry commands (`create`, `update`, `remove`) do carry it | Core `components/config/*_registry.py` |
| `GET /api/states` returns every entity; for a non-administrator it returns only entities the user may read. `GET /api/states/<id>` is 404 when missing and 401 when the user may not read it | Core `components/api/__init__.py` (`APIStatesView`, `APIEntityStateView`) |
| `GET /api/history/period/<start>` **requires** `filter_entity_id`, lower-cases it, filters it by the user's read permission, defaults `end_time` to start plus one day, and treats `significant_changes_only` as on unless it is `0`. `minimal_response` and `no_attributes` are flags tested by presence. The result is a list with one list per entity | Docs `docs/api/rest.md`; Core `components/history/__init__.py` |
| `GET /api/logbook/<start>` takes optional `entity` and `end_time`. **The REST logbook view does no per-user filtering**: it never consults the user's permissions, and without `entity` it returns the whole window for every entity. Each entry carries `context_*` fields that can name other entities | Core `components/logbook/rest_api.py`; Docs `docs/api/rest.md` |
| `GET /api/services` returns a list of `{"domain", "services"}`; the docs show service names as a list, the source puts a mapping of descriptions there. The plugin accepts both | Docs `docs/api/rest.md`; Core `components/api/__init__.py` (`async_services_json`) |
| `POST /api/services/<domain>/<service>` takes `service_data` (which may itself hold `entity_id`, `area_id`, `device_id`, `label_id`), returns the list of states changed under the call's context (which can include entities the caller never named), and needs `?return_response` for services that return data | Docs `docs/api/rest.md`; Core `components/api/__init__.py` |
| Automation services are `turn_on`, `turn_off` (field `stop_actions`, default true), `toggle`, `trigger` (field `skip_condition`, **default true**, which runs the actions while ignoring the automation's conditions) and `reload` | Core `components/automation/services.yaml` |
| Categories are read over the same read-only WebSocket: `config/category_registry/list` with `scope` (`automation`), not administrator-only, entries `{category_id, name, icon}`; each entity-registry entry carries `categories` (for example `{"automation": "<id>"}`) | Core `components/config/category_registry.py`, `helpers/category_registry.py`, `helpers/entity_registry.py` |
| Automation definitions are edited through `/api/config/automation/config/<id>` (GET, POST, DELETE), **all administrator-only**. POST validates the whole automation, rewrites `automations.yaml` with that id's config replaced, and reloads it; DELETE removes the YAML entry and the entity-registry entry | Core `components/config/view.py`, `components/config/automation.py` |
| `POST /api/template` is administrator-only and runs arbitrary templates. Not used | Core `components/api/__init__.py` (`@require_admin`) |
| `GET /api/config` returns version, latitude, longitude, elevation, `config_dir`, URLs. The plugin reads only `version` | Docs `docs/api/rest.md` |
| A wrong token counts toward Home Assistant's IP ban only when `login_attempts_threshold` is set; the default means no banning | Core `components/http/ban.py`, `components/http/__init__.py` (`NO_LOGIN_ATTEMPT_THRESHOLD`) |
| The host image has no WebSocket library. `httpx2` 2.16.0 (already in the image) has a WebSocket client, `AsyncClient.websocket()`, but it imports `wsproto`, which is an optional extra and not installed | `requirements.lock` in mcpopus; `httpx2/_client.py`, `httpx2/websockets/_api.py` read from the installed wheel |

### Transport decision

REST for everything except areas; WebSocket, read-only and only for the three registry lists, for area and device data. Reasons: REST
is simple, stateless and bounded for states, history, logbook and services; areas are in the brief and WebSocket is the only allowed
route to them; the WebSocket is opened per refresh (60 second cache), authenticates, issues three list commands, and closes, so no
long-lived connection or event subscription exists. Using WebSocket for everything was rejected: it needs a persistent session for no
gain, and the REST history endpoint is the one that applies the user's read permissions.

### The host change this needs

Area features need `wsproto` in the host image. Smallest change (in mcpopus, not made here because the brief says to stop and report):
add `wsproto==1.3.2` to `dependencies` in `pyproject.toml`, regenerate `requirements.lock`, add `wsproto` to the `cmd_smoke` import list in
`src/mcpopus/cli.py`. Until then the plugin still works: `status` says area information is unavailable and says why, searching and
reading entities works without areas, and only the area filter and `list_areas` return an error. The test environment installs
`wsproto==1.3.2` (see `requirements-dev.txt`).

## 3. Unverified

- Everything against a real Home Assistant. The fake follows the documentation and the source, which is not the same as the product.
- Whether the release Alex runs behaves like the `dev` branch read here (field names in the registry lists, `/api/logbook` still
  unfiltered, `significant_changes_only` default).
- Whether Home Assistant lets a non-administrator user be limited to certain entities (the source has a per-entity permission check
  but this build did not find a way to configure it in the user interface). The plugin does not rely on it.
- The history and logbook endpoints need the `recorder`, `history` and `logbook` integrations; the behaviour when one is off is not
  checked beyond a generic HTTP error message.
- Home Assistant's WebSocket message-size and rate limits for large registries; the plugin caps the reply it will accept
  (`max_response_bytes`).
- Whether `ws://` and `wss://` URLs derived from `base_url` work behind Alex's reverse proxy (the proxy must forward WebSocket upgrades).

## 4. Architecture

```
plugins/homeassistant/
  plugin.toml     manifest
  __init__.py     Config, the Plugin class and its tools, setup and teardown
  client.py       REST and WebSocket access, error mapping, caches, the token-refusal pause
  policy.py       entity and service policy (globs, domain deny list), hidden-reference detection
  text.py         bounding and cleaning of names, attributes and messages
tests/fake_ha.py  fake Home Assistant (REST and WebSocket, records every request and every possible write)
```

- The plugin owns one `httpx2.AsyncClient`, built in `setup` with its own TLS context (the host's shared client cannot take a custom
  certificate authority or a verify flag), closed in `teardown`. `setup` does no network work: problems appear on use, as a tool error.
- The token is read from the config at the moment of each request and is never stored on the plugin.
- States are cached for 3 seconds and the registry for 60, so a burst of tool calls does not become a burst of requests. A failing
  registry is not retried for 20 seconds.
- After Home Assistant refuses the token, all calls are paused for 60 seconds, so a revoked token cannot turn into a stream of failed
  logins (relevant if Home Assistant's IP ban is switched on).
- Redirects are not followed. Replies are read as a stream and refused beyond `max_response_bytes`.

## 5. Policy

Order of checks for any entity: the id must be well formed; its domain must not be in `domain_denylist`; it must not match any
`entity_denylist` pattern; and, if `entity_allowlist` is not empty, it must match one of its patterns. The deny rules always win.
Patterns are lower-case globs (`light.*`, `sensor.kitchen_?`). Policy is checked **before** anything is fetched, listed, counted or
queried, and a hidden entity gets the same answer as a missing one:

| Tool | Hidden or missing entity |
| --- | --- |
| `get_entity` | `No such entity.` |
| `get_states` | `- <id> \| not found` |
| `get_history` | `No history found for <id> in that window.` (Home Assistant is not asked about a hidden entity) |
| `get_logbook` | `No logbook entries for <id> in that window.` |
| `search_entities`, `list_areas`, `status` | never listed or counted; counts are of the visible set only, and no count of hidden items is given |

Where else a hidden entity could leak, and what is done:

- **Group and similar attributes** that list member ids (`entity_id: [...]`): hidden ids are removed from any attribute value.
- **Logbook**: only entries for the entity asked for; every `context_*` field is dropped (they name other entities); entries whose
  text names a hidden entity id are dropped. This is a heuristic on ids: a message that names a hidden thing by its friendly name
  cannot be caught. For a strong guarantee, use an allow list rather than relying on the deny lists.
- **History**: one entity per call, `no_attributes` and `minimal_response` always set, so no attribute reaches the output.
- **Areas**: an area is listed even when all its entities are hidden (with 0 entities); area names are not entity data.
- **Attribute redaction**: `redact_attributes` (default access_token, entity_picture, latitude, longitude, gps_accuracy, ip_address,
  mac, mac_address, token, password) shows `[redacted]` instead of the value.

Suggested defaults, for Alex to change: `domain_denylist` = lock, alarm_control_panel, camera, person, device_tracker, notify. These are
the entities where a wrong answer or a wrong action matters most. They are suggestions, not facts about Alex's home. `cover` (garage
doors), `climate` and `switch` are not denied by default, and need an explicit decision from Alex before they go on any write
allowlist (section 9).

## 6. Tools

All are tier `read` except `call_service` (tier `write`). All return text that came from Home Assistant, so all except `status` are `untrusted=True` (the host wraps them
as data and caps their size).

| Tool | Arguments | Returns |
| --- | --- | --- |
| `status` | none | Version, token works, visible entity count and counts by domain, whether area data is available, the policy in force |
| `search_entities` | `query`, `domain`, `area`, `device_class`, `state`, `limit`, `offset` | Matching visible entities: id, name, state, area; paged; `limit` capped by `max_results` |
| `get_entity` | `entity_id` | State, name, area, device class, timestamps, attributes (redacted, at most 40, 3000 characters) |
| `get_states` | `entity_ids` (at most 50) | One line per entity; unknown and hidden alike `not found` |
| `get_history` | `entity_id`, `start`, `end`, `hours` | State changes, at most 200, window capped by `max_history_hours` |
| `get_logbook` | `entity_id`, `start`, `end`, `hours` | Entries for that entity, at most 100, context removed |
| `list_areas` | `area`, `limit`, `offset` | Areas with visible entity counts, or the visible entities in one area |
| `list_services` | `domain` | The services in `allowed_services` with their descriptions, and the entities `call_service` may act on. Empty by default |
| `call_service` | `domain`, `service`, `entity_id`, `data` | **Tier write.** One service on one named entity; returns its state before and after. Section 9 |
| `list_automations` | `category`, `state`, `limit`, `offset` | Automations with state, last run, running count, category, and whether this connector may control each. Read |
| `trigger_automation` | `entity_id` | **Tier write.** Runs one automation with its conditions respected. Section 9b |
| `set_automation_enabled` | `entity_id`, `enabled` | **Tier write.** Enables or disables one automation without stopping a run in progress. Section 9b |

Not built, on purpose: the template endpoint, a raw API pass-through, any call to an arbitrary URL, the camera proxy, the error log,
event firing, state setting, WebSocket subscriptions, group, area, device and "all" targets, and responses from services (`return_response`).

## 7. Configuration

| Setting | Default | Notes |
| --- | --- | --- |
| `base_url` | required | `http(s)://host[:port]`, no credentials, query or path. Plain `http` sends the token unencrypted: acceptable only on a network you trust |
| `token` | required (secret) | Long-lived access token. Create it on a dedicated, non-administrator user, so the token cannot do more than that user can |
| `verify_tls` | true | Turning it off removes the protection of https |
| `ca_certificate` | empty | PEM text of a private certificate authority |
| `entity_allowlist` | empty | Globs. Empty means all entities not denied |
| `entity_denylist` | empty | Globs. Always wins |
| `domain_denylist` | the six domains above | Replacing the list removes the defaults |
| `allowed_services` | empty | `domain.service` pairs, exact; refused at load if the domain is in `domain_denylist`. Empty means no service may be called |
| `write_entity_allowlist` | empty | Globs for the entities `call_service` may act on. Separate from what can be read: an entity must also be visible. Empty means none. Refused at load if a pattern's domain is in `domain_denylist` |
| `automation_categories` | empty | Names of Home Assistant automation categories (case-insensitive). Automations in these categories can be triggered, enabled and disabled. Empty means none, except automations matching `write_entity_allowlist` |
| `max_writes_per_minute` | 10 | 1 to 120, on top of the host's own limit |
| `max_results` | 50 | 1 to 500 |
| `max_history_hours` | 72 | 1 to 720 |
| `redact_attributes` | the list in section 5 | Case-insensitive |
| `instructions` | empty | Text shown to the client with the connector |
| `timeout_seconds` | 15 | 3 to 120 |
| `max_response_bytes` | 12 000 000 | Largest reply accepted (the full entity list is the biggest) |

Values are trimmed. Invalid values stop the plugin loading with a message that names the field, never the value.

## 8. Failure messages

Every message names a setting or says what to check; none contains the URL, the token or text from Home Assistant.

| Situation | Message (start) |
| --- | --- |
| Token refused | `Home Assistant refused the access token. Check the token setting`; later calls: `... paused for 60 seconds ...` |
| Permission refused (403) | `... refused permission for this request. The token's user may not be allowed to read it.` |
| Host unreachable | `Cannot connect to Home Assistant. Check the base_url setting ...` |
| Timeout | `Home Assistant did not answer in time. Check the base_url and timeout_seconds settings.` |
| Certificate | `Home Assistant's certificate was not accepted. Check base_url, or set ca_certificate ...` |
| Redirect | `Home Assistant redirected the request. Check the base_url setting ...` |
| Not JSON | `... returned something that is not JSON. Check that base_url points at Home Assistant ...` |
| Reply too large | `... larger than the max_response_bytes setting allows ...` |
| Areas unavailable | `Area and device data need the wsproto library, which this host does not have ...` (or the WebSocket variants) |
| Window too long | `That window is longer than this connector allows (72 hours, the max_history_hours setting).` |

## 9. Writes (`call_service`, 0.2.0)

Three independent switches must all be on before anything can change, and each refusal happens before anything is sent to Home Assistant:

1. The owner ticks `call_service` under Policy in the host's admin UI **and** turns read-only off (host policy, enforced by the host).
2. The service is listed in `allowed_services` (exact `domain.service`; the domain must not be in `domain_denylist`).
3. The entity matches `write_entity_allowlist` and is also visible under the read policy.

Checks, in order, and what each returns (the first four are the same whatever the cause):

| Check | Result |
| --- | --- |
| Service not allowed, in a denied domain, malformed, or not offered by Home Assistant | `No such service.` (one answer for all four) |
| `entity_id` not one well-formed id (so no "all", list, comma list, wildcard or area) | error saying what an id looks like |
| Entity domain differs from the service domain (so `homeassistant.turn_off` cannot reach another domain) | error |
| Entity hidden by policy, or does not exist (checked against the visible states, before the write list) | `No such entity.` |
| Entity visible and exists but not on the write list | `This connector may not change that entity ...` (the entity is visible anyway, so nothing hidden is revealed) |
| `data` not an object, over 20 fields, over 2000 characters, bad field names, naming a target (`entity_id`, `area_id`, `device_id`, `label_id`, `floor_id`, `target`), or holding another entity's id | error |
| More than `max_writes_per_minute` calls in the last minute | error naming the setting (only calls that reach Home Assistant count) |

Then: it reads the entity's state, sends `POST /api/services/<domain>/<service>` with `{...data, "entity_id": <entity>}`, reads the state again,
and returns both. States Home Assistant reports as changed under the call's context are listed only if the policy shows them (an
automation may have moved a hidden lock). A timeout says the action may or may not have happened. Nothing is retried. The host's audit
log records the call with argument names only; the plugin also counts `service_calls` in the host statistics.

Writes and prompt injection: this plugin reads text from Home Assistant (names, attributes, logbook) that people and devices set, and
also has a write tool, which the plugin rules say to avoid where possible. The brief asked for both in one plugin, so the controls are:
a narrow write list, exact service pairs, one entity per call, before and after state, a rate limit, and the host's per-tool tick. Also
set `call_service` to **Needs approval** in claude.ai's connector tool permissions, so a human confirms each call.

Decisions taken (6 and 7 November 2026): Alex allowed `light.turn_on`, `light.turn_off`, `switch.turn_on`, `switch.turn_off`,
`cover.open_cover`, `cover.close_cover`, `climate.set_temperature`, and chose `light.*` as the only entities to act on. So the cover,
switch and climate services are allowed but have no allowed target: nothing but lights can change. Adding a switch, cover or climate
entity to `write_entity_allowlist` is a separate decision, and a garage door in particular is a physical-security action.

Not supported: `return_response` services, group, area, device or label targets, services in another domain than the entity's, scripts,
scenes and automations (their domains are not on the allowlist, and an allowlisted pair must match the entity's domain).

## 9b. Automations: list, trigger, enable and disable (0.3.0)

An automation can do anything, including things the entity policy hides, so **the category is the control**: only automations the owner put
in a category named in `automation_categories` can be controlled. Reading and listing follow the normal read policy.

- `list_automations` shows every visible automation with a `controllable: yes/no` flag, so the model can tell which it may act on.
- `trigger_automation` always sends `skip_condition: false`, so the automation's own conditions still apply. Home Assistant's default
  is to skip them; the plugin never does, and there is no way to ask it to. `set_automation_enabled` disables with `stop_actions: false`,
  so a run in progress is left to finish.
- Each tool is its own host tick, so the owner can allow listing and enabling but not triggering, or the reverse. They share the
  `call_service` rate limit, are never retried, and return state, last run and running count before and after, plus other visible
  entities the call changed.
- Order of checks: well-formed automation id; entity exists and is visible (`No such entity.`, the same for hidden and missing); then
  the category or write-pattern check (`This connector may not control that automation ...`, said only about an automation that is
  visible anyway). If category data is unavailable (no `wsproto`, see section 2) the answer says so and nothing is controllable except
  automations matching `write_entity_allowlist`: it fails closed.
- `automation.*` services cannot be put in `allowed_services` (refused at load), so `call_service` cannot be used to bypass the
  condition and category rules.
- The fence is only as safe as the category's contents. A trigger runs the automation's actions, which can touch locks or anything else.
  Put an automation in the category only if you are content for Claude to run it.

## 9c. Reading and editing automation definitions (designed, not built)

Alex asked for reading definitions and for creating, editing and deleting automations. Not built yet, because it changes the risk
picture and needs decisions. What was verified (section 2): the config API is administrator-only, replaces a whole automation per save,
and rewrites `automations.yaml`. What follows is the design to build once the decisions below are made.

Why it is different: an automation Claude writes runs later with no plugin policy in the loop. A created automation could unlock a door
on a schedule. The plugin's entity and service lists cannot constrain an automation after it is saved, so the constraint has to be on
what may be saved.

Proposed controls:

1. **A second, optional secret, `admin_token`**, used only for these tools. The ordinary `token` stays a non-administrator user's, so a
   leak of it still cannot edit anything. Without `admin_token` the tools are not usable.
2. **Fence by config id, not category**: Claude may edit or delete only automations whose config `id` starts with `claude_`, and creates
   only such ids. Human-written automations can be read (if the definition passes the filter below) but never changed or deleted. (A
   category cannot be assigned through the config API; assigning one needs an administrator registry call.)
3. **Strict subset on save.** Reject anything that is not: triggers of type `state`, `numeric_state`, `time`, `time_pattern`, `sun` on
   visible entities; conditions of type `state`, `numeric_state`, `time`, `sun`; actions that are only `action: domain.service` calls
   on pairs in `allowed_services` against entities in `write_entity_allowlist` (the same checks as `call_service`), or `delay`. No
   templates anywhere (any string containing `{{` or `{%` is refused), no scripts, scenes, `choose`, `repeat`, `event`, `webhook` or
   `homeassistant` services.
4. **New automations start disabled** (`initial_state: false`, to be verified in Home Assistant's automation schema), and the tool tells
   the owner to review and enable it in Home Assistant, or to call `set_automation_enabled` if it is in an allowed category.
5. **Reading a definition** (read tier, administrator token): refused as "not available" (the same text as missing) if the definition
   mentions a hidden entity, a denied domain, or contains templates, so definitions cannot be used to find hidden things.
6. Tiers: `save_automation` write, `delete_automation` admin. The admin audit lines record the automation id and nothing else.
7. A read-modify-write on `automations.yaml` loses hand-written YAML comments (Home Assistant rewrites the file). The tool says so.

Decisions needed from Alex before this is built: whether to create an administrator token at all; the `claude_` id prefix rule; whether
the subset above is acceptable or too narrow; whether created automations must start disabled.

## 10. Acceptance checklist (Alex, against the real Home Assistant)

Set-up: in Home Assistant create a **non-administrator user** for this connector and, from that user's profile, a long-lived access
token. In the host's admin UI install `homeassistant` (Plugins, Fetch, Install, trust), open it, and enter:

| Setting | Value |
| --- | --- |
| `base_url` | Your Home Assistant address, for example `http://192.168.1.20:8123` |
| `token` | The token (stored encrypted; the form never shows it back) |
| `domain_denylist` | Keep the six suggested domains, or change them |
| `entity_denylist` | Add one entity you know exists and want hidden, to test it (for example one light) |
| others | Leave the defaults |

Save and reload, tick the read tools under Policy, add `https://<host>/homeassistant` as a connector in claude.ai. Then ask Claude:

1. "Run status." Expect: the Home Assistant version, "the access token works", plausible counts by domain, **no** lock, alarm_control_panel,
   camera, person, device_tracker or notify in the domain list, and "Area information: available" or a reason.
2. "Search for entities with 'kitchen' in the name." Expect results with areas (or without, if area data is unavailable).
3. "Get the history of <a temperature sensor> for the last 6 hours." Expect a short list of times and values.
4. "Get the state of <the entity you added to entity_denylist>" and "of sensor.this_does_not_exist". Expect the same answer for both
   (`No such entity.`), and the entity absent from a search for its name and from `list_areas`.
5. "Get the state of <a lock or camera in your home>." Expect `No such entity.`
6. "Call a service to turn a light on." Expect Claude to say it has no tool for that.
7. In Home Assistant open the user's profile and check the token's "last used" and, in the logbook, that nothing was changed.
8. Revoke the token in Home Assistant and run status again. Expect the refused-token message, then a pause message on the next call.

If areas are unavailable, that is the missing `wsproto` library (section 2); everything else must still work.

## 10b. Acceptance checklist for `call_service` (Alex, against the real Home Assistant)

In the plugin settings enter: `allowed_services` = the seven pairs in section 9 (one per line); `write_entity_allowlist` = `light.*`;
leave `max_writes_per_minute` at 10. Save and reload. Under Policy, turn read-only off and tick `call_service`. In claude.ai, open the
connector's tool permissions and set Call service to Needs approval. Update the plugin first (Fetch, Apply update) so it runs 0.2.0. Then:

1. Ask Claude to run `list_services`. Expect the seven services, `light.*` as the only target, and the cover and climate services marked
   "not offered" if you have none.
2. Ask: "Turn on light.inside_porch with call_service." Approve it. Expect before and after states and the light to change.
3. Ask Claude to turn it off again.
4. Ask it to set brightness 50 on the same light, with `data` {"brightness": 128}. Expect the new brightness in the after state.
5. Ask it to call `light.toggle` (not allowed) and `lock.unlock` on a lock. Expect `No such service.` for both and no change.
6. Ask it to turn on a **switch** that exists. Expect "may not change that entity". Ask it to turn on `person.vicki`. Expect "No such entity."
7. Ask it to turn on "all lights", then give `entity_id` "light.*". Expect refusals.
8. Ask it to turn on a light but add `area_id` to the data. Expect a refusal.
9. Ask for 12 calls in a minute (for example repeated on and off). Expect the rate-limit message after 10.
10. In Home Assistant check the logbook: only the changes you approved, made by the connector's user.
11. In the host's Policy page untick `call_service`, reload, and ask again. Expect Claude to say it has no such tool.

## 10c. Acceptance checklist for the automation tools (Alex, against the real Home Assistant)

In Home Assistant create a category called `Claude` for automations (Settings, Automations, Categories) and put **one harmless
automation** in it (for example one that turns the porch light on and off). Update the plugin (0.3.0), set `automation_categories` to
`Claude`, under Policy tick `trigger_automation` and `set_automation_enabled`, and set both to Needs approval in claude.ai. Area and
category data need `wsproto` in the host image: without it the tools refuse everything and say why. Then:

1. "List my automations." Expect all of them, with the Claude-category one marked `controllable: yes` and the rest `no`.
2. "Trigger <the harmless automation>." Approve. Expect its last-run time to change and the light to react. If its conditions are not
   met nothing happens, and that is correct.
3. "Disable it", then "enable it". Expect off, then on.
4. "Trigger <an automation outside the category>." Expect "may not control that automation" and no change.
5. "Trigger automation.does_not_exist" and one you hid in `entity_denylist`. Expect the same "No such entity." for both.
6. In the logbook confirm only the approved actions happened, and that the automation's conditions were respected (put a false
   condition on it once and trigger: nothing should run).

## 11. Live check results

7 November 2026, plugin 0.1.0 on Alex's host against his real Home Assistant, through a claude.ai connector, by the 18-step prompt.
No step failed. Verified live: status (version, token works, domain list without the denied domains, no count of hidden items); paged
search; device-class filter (a dew point sensor counts as `temperature`, which is Home Assistant's own device class); `get_entity` with
redaction (no location, token or picture values); `get_states` with a missing entity; history over 6 hours (nine changes) and the
`max_history_hours` refusal; a clear error for a bad date; `person.vicki` (hidden by the `person` domain rule) answering exactly like a
missing entity in `get_entity`, `get_states`, `get_history` and `get_logbook`; no match for its name in search; clear errors for malformed
ids; no tool that can change anything.

Not covered by this run:

- **Areas** (steps 4, 12 second half, 14): skipped, because the host image has no `wsproto` (section 2). Expected.
- **The entity pattern rule** live: the hidden entity was hidden by domain; `entity_denylist` and `entity_allowlist` were not exercised against the real system.
- **The logbook filters** live: the one logbook query returned no entries, so the `context_*` stripping and hidden-reference dropping have only been seen against the fake.
- **Step 16's exact boundary** (101 characters): the client did not count its input; covered by the automated test.
- **Revoked-token behaviour** (the optional check), and the 3 other items under section 3 (released-version differences, recorder off, reverse proxy).
- **DENIED and LOCKED were the same entity**, so only the `person` domain was tested live.

