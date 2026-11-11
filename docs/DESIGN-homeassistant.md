# homeassistant: design

Written 6 November 2026. Specification of the `homeassistant` plugin (endpoint `/homeassistant`), version 0.5.0 (0.4.4 clarified the allow lists and the definition messages, section 5a and 9c control 9; 0.5.0 adds `create_helper`, section 9d). Reads, plus write tools (`call_service`, `trigger_automation`, `set_automation_enabled`, `save_automation`, `delete_automation`) that are off until the owner enables each by name and names what they may touch. Conventions
and the rules every plugin follows are in [DESIGN.md](DESIGN.md) section 1; the host's plugin contract is in the mcpopus repository
(`docs/PLUGINS.md`, `docs/SECURITY.md`). Status: the read tools passed a live check on 7 November 2026 (section 11). `call_service` (0.2.0), the automation tools (0.3.0) and the definition tools (0.4.0) are
built and tested against a fake Home Assistant only; their live checks are sections 10b, 10c and 10d. The definition tools were run live on 10 November 2026 (section 11); section 10f is the corrected second run.

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
| A Home Assistant user can be marked **local only**: authentication is then refused over a cloud connection (Nabu Casa) and from any address that is not on a local network | Core `components/http/auth_util.py` (`async_user_not_allowed_do_auth`, `local_only`, `is_local`) |
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

Area features need `wsproto`. **Since host 0.3.0 (10 Nov 2026) the plugin lists `wsproto==1.3.2` and `h11==0.16.0` in its own `requirements` (0.4.3) and the host installs them; the host image no longer carries `wsproto`.** The earlier host-side option, kept for history:
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

## 5a. Which setting does what (0.4.4)

Four settings decide whether Claude can see, and change, an entity. They are separate on purpose: a home is usually read widely (674 to 1003 entities)
and changed narrowly (a few lights), and one shared list would force either hiding the rest of the home or making everything visible changeable.

| Setting | Question it answers | Empty means | Hides things? |
| --- | --- | --- | --- |
| `entity_allowlist` | Which entities may Claude see at all? | everything not denied | **Yes**: once set, everything that does not match is hidden |
| `entity_denylist`, `domain_denylist` | Which entities are never visible? | nothing extra (the domain list defaults to six domains) | Yes, and always wins |
| `allowed_services` | Which `domain.service` pairs may be called, or used in a saved automation? | no service | No |
| `write_entity_allowlist` | Which visible entities may a call or a saved automation's action change? | none | No |

Order for any use of an entity: it must pass the denied domains, the deny patterns and the allow list (visible), exist, and for a change match the write
list; the service must be in `allowed_services` and in the entity's domain.

**Worked example, the doorbell chime** (trigger `event.doorbell_doorbell`, action `input_boolean.turn_on`, a 3 second delay, `input_boolean.turn_off` on `input_boolean.doorbell_chime`):

- `allowed_services`: `input_boolean.turn_on`, `input_boolean.turn_off`.
- `write_entity_allowlist`: `input_boolean.doorbell_chime`.
- `entity_allowlist`: leave **empty**. Both entities are then visible. If you do set it, it must match **both** the trigger and the action entity (`event.doorbell_doorbell` and `input_boolean.doorbell_chime`), and then everything else is hidden.
- The helper `input_boolean.doorbell_chime` must already exist in Home Assistant (Settings, Devices and services, Helpers), or be created by `create_helper` (section 9d; that makes `input_boolean.claude_doorbell_chime`, so use that id in the automation and in `write_entity_allowlist`). If it is missing or blocked, `save_automation` says which: it names the policy rule, or says the entity does not exist.
- The trigger is a **state trigger with no `to`**. An event entity's state is the timestamp of its last event (`None`, shown as unknown, before the first), never `on` or `off`, so `to: "on"` would never match. A state trigger with only an `entity_id` fires on every state change, attribute-only changes included (Core `components/event/entity.py`, `components/homeassistant/triggers/state.py`, read 11 November 2026).

The failure reported on 11 November 2026 was an `entity_allowlist` holding one pattern that matched nothing (it showed as `1 allow pattern(s)` and `Visible entities: 0`): the owner's intent was a write permission, which belongs in `write_entity_allowlist`. From 0.4.4 `status` says which setting is which, warns when the allow list matches nothing, and says how many visible entities match the write patterns.

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
| `get_automation_definition` | `entity_id` | Read (needs the token's user to be an administrator). The triggers, conditions and actions of one automation; refused as "No definition available." if it contains templates or names anything hidden. Section 9c |
| `save_automation` | `automation_id`, `alias`, `triggers`, `conditions`, `actions`, `description`, `mode` | **Tier write** (needs the token's user to be an administrator). Creates or replaces an automation whose id starts with the prefix; strict subset; starts disabled. Section 9c |
| `create_helper` | `helper_id`, `initial` | **Tier write** (needs the token's user to be an administrator). Creates one `input_boolean` named `<helper_id_prefix><helper_id>` and puts it in the owner's helper category. Section 9d |
| `delete_automation` | `automation_id` | **Tier admin** (needs the token's user to be an administrator). Deletes an automation whose id starts with the prefix. Section 9c |

Not built, on purpose: the template endpoint, a raw API pass-through, any call to an arbitrary URL, the camera proxy, the error log,
event firing, state setting, WebSocket subscriptions, group, area, device and "all" targets, and responses from services (`return_response`).

## 7. Configuration

| Setting | Default | Notes |
| --- | --- | --- |
| `base_url` | required | `http(s)://host[:port]`, no credentials, query or path. Plain `http` sends the token unencrypted: acceptable only on a network you trust |
| `token` | required (secret) | Long-lived access token of a dedicated Home Assistant user. For the read tools and `call_service`, a non-administrator is enough and safer. The automation definition tools (section 9c) need the user to be an **administrator**: then also mark the user **local only** (section 9c, control 1) |
| `verify_tls` | true | Turning it off removes the protection of https |
| `ca_certificate` | empty | PEM text of a private certificate authority |
| `entity_allowlist` | empty | Globs. **The read filter.** Empty means all entities not denied. If it lists anything, **only** entities matching it are visible and everything else is hidden from every tool, including the entities an automation trigger would watch. It does not mean "entities Claude may change" (that is `write_entity_allowlist`). See section 5a |
| `entity_denylist` | empty | Globs. Always wins |
| `domain_denylist` | the six domains above | Replacing the list removes the defaults |
| `allowed_services` | empty | `domain.service` pairs, exact; refused at load if the domain is in `domain_denylist`. Empty means no service may be called |
| `write_entity_allowlist` | empty | Globs. **The write filter**: the entities `call_service` and the actions of saved automations may change. It hides nothing. An entity must match it **and** be visible under the read filter. Empty means none. Refused at load if a pattern's domain is in `domain_denylist` |
| `automation_id_prefix` | `claude_` | Ids Claude may create, change or delete must start with this. Human-written automations never do |
| `automation_categories` | empty | Names of Home Assistant automation categories (case-insensitive). Automations in these categories can be triggered, enabled and disabled. Empty means none, except automations matching `write_entity_allowlist` |
| `helper_id_prefix` | `claude_` | Helpers `create_helper` makes are named `<prefix><helper_id>`. 1 to 20 characters, lower-case letters, digits and underscores, starting with a letter (section 9d) |
| `helper_category` | empty | Name of an existing Home Assistant **helper** category the new helpers are put in. Empty means `create_helper` refuses (section 9d) |
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
| Areas unavailable | `wsproto is missing: install the plugin's packages from its page to get area and device data. Everything else works.` (or the WebSocket variants) |
| Entity unusable in a saved automation | One of four, chosen by the settings and the id's text only: `cannot be used: its domain (lock) is in the domain_denylist setting`, `... matches the entity_denylist setting`, `... is outside the entity_allowlist setting, so it is hidden (that list is the read filter ...)`, or `does not exist in Home Assistant (or is not available)` (0.4.4; see 9c control 9) |
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

## 9c. Reading and editing automation definitions (0.4.0)

Alex asked for reading definitions and for creating, editing and deleting automations, and chose to port the capability into this plugin
rather than proxy ha-mcp (9 November 2026). Behaviours learned from ha-mcp are credited in NOTICE.md; the code is new.

Why it is different: an automation Claude writes runs later with no plugin policy in the loop. A created automation could unlock a door on
a schedule, so what may be saved is the control. What was verified (section 2): the config API is administrator-only, replaces a whole
automation per save, rewrites `automations.yaml`, and Home Assistant accepts both `trigger:` and `platform:` for trigger types and `action:`
for service calls (Core `helpers/config_validation.py`, `components/automation/config.py`).

Controls, all enforced by the plugin before anything is sent:

1. **One token, and an administrator user to use the definition tools.** Home Assistant's automation config API is administrator-only, so those
   three tools only work if the token's user is an administrator. Alex chose (9 November 2026) to use the one token rather than a second
   secret. That makes a leaked token powerful, so the recommended set-up is a **dedicated administrator user marked "local access only"**
   (user settings in Home Assistant). Home Assistant then refuses that user's token from any address that is not on a local network and over
   Nabu Casa (verified in Core, section 2), so using a stolen token needs a foothold inside the network, as Alex observed. The host reaches
   Home Assistant over the LAN, which is local. If the host reaches it through a reverse proxy, the proxy's address must count as local;
   otherwise sign-in fails and `status` shows the refusal. A non-administrator token still works for everything except these tools: a
   refused config call pauses **only** the config calls for 60 seconds (Home Assistant counts refused calls towards its IP ban when one is
   configured), and every other tool carries on.
2. **Fence by config id.** Claude may create, replace or delete only ids matching `<prefix><1 to 40 of a-z 0-9 _>` (default `claude_`).
   Human-written automations never match, so they cannot be changed or deleted. They can be *read* (see 5).
3. **Strict subset, built from a whitelist (never passed through).** Triggers: `state`, `numeric_state`, `time`, `time_pattern`, `sun`, only on
   entities that exist and are visible. Conditions: `state`, `numeric_state`, `time`, `sun`. Actions: an allowed service
   (`allowed_services`) on **one** entity that is on the write list and in the service's domain, with `data` checked as for `call_service`, or a
   `delay` of at most an hour. Never: templates anywhere (`{{`, `{%`, `{#`), events, webhooks, `choose`, `if`, `repeat`, `wait_template`,
   variables, scripts, scenes, other automations, area, device or "all" targets. At most 5 triggers, 5 conditions and 10 actions; mode
   `single` or `restart`. An automation whose action changes an entity its own trigger watches is refused (it could trigger itself).
4. **New automations start disabled** (`initial_state: false`, which Home Assistant's schema accepts). A later edit keeps whatever
   `initial_state` the stored config already has, so the owner's choice survives. To make a Claude-created automation start enabled, the
   owner removes `initial_state` in Home Assistant's YAML editor (or enables it in the interface for the current run).
5. **Reading a definition** (read tier, administrator token): the definition is shown only if the automation is visible, has no templates, and
   no string in it names an entity hidden by the policy (including denied domains such as `lock.`). Otherwise the answer is
   `No definition available.`, identical for missing, hidden, templated, unreadable and non-automation ids.
6. **Name clashes**: a new alias may not equal an existing automation's name (as Home Assistant would otherwise rename it silently).
7. Tiers: `save_automation` write, `delete_automation` admin; both share the `call_service` rate limit, are never retried, and the audit log
   records argument names only. Home Assistant's own error text is never repeated.
8. Home Assistant rewrites `automations.yaml` when saving or deleting, so comments in that file are not kept; every save says so.

9. **Which rule blocked an entity (0.4.4).** In the messages of `save_automation` only, an entity id that fails the policy is refused with the rule named (denied
   domain, deny pattern, or outside the allow list), and an id that passes the policy but is not in Home Assistant is refused as missing. Whether the rule applies
   depends on the id's text and the owner's settings alone, never on whether Home Assistant has the entity, so a hidden entity and a made-up id in the same
   rule's reach get the same message (a test compares them), and what exists behind the policy is not revealed. The price is that the policy's shape can be
   probed (an id is "in a denied domain" or "matches a deny pattern"); the denied domains are already in `status`, and no pattern text is ever shown. This
   softens the older wording "a hidden entity is indistinguishable from a missing one" for this one tool, at the owner's request (11 November 2026). The read tools,
   `call_service` and the automation control tools are unchanged and still answer `No such entity.` for both. To restore the single answer, make `Context._unavailable` return the missing-entity message for every case.
10. **Event entities** are valid state-trigger sources (nothing in the subset excludes a domain other than those the owner denied); use no `to` (section 5a).

Not built: editing automations written by hand (the prefix fence refuses), blueprints, scripts, scenes, traces. Home Assistant 2026 versions
older than the one read here may differ in field names (unverified).

## 9d. Creating helpers (`create_helper`, 0.5.0)

Asked for on 11 November 2026: Claude may create helpers, with a prefix and in a category. Built for `input_boolean` only (the helper type the doorbell chime needed);
other helper types are separate decisions. There is no update or delete tool: removing a helper is done in Home Assistant, which keeps this to one new kind of change.

What was verified (Core and frontend source, read 11 November 2026):

| Fact | Source |
| --- | --- |
| Helpers are created over the WebSocket command `input_boolean/create` (REST has none). It is registered with `require_admin` unconditionally, as are update and delete | Core `helpers/collection.py` (`StorageCollectionWebsocket.async_setup`) |
| The fields are `name` (required, non-empty), `initial` (boolean, optional) and `icon` (optional). The reply is the created item | Core `components/input_boolean/__init__.py` (`STORAGE_FIELDS`), `helpers/collection.py` (`ws_create_item`) |
| The item id is the slug of the name, with `_2`, `_3`... added if it is taken; the entity id is `input_boolean.<item id>` (the entity module that sets the unique id was not read, so that last step is checked after creation) | Core `helpers/collection.py` (`IDManager.generate_id`), `input_boolean/__init__.py` (`_get_suggested_id` returns the name) |
| `config/entity_registry/update` takes `categories`, a map of scope to category id (`null` removes); passing an id adds or moves; scopes not named are left alone. It is administrator-only. `config/entity_registry/get` and the category list are not | Core `components/config/entity_registry.py`, `category_registry.py` |
| The category scope for helpers is `"helpers"` (`entity.categories.helpers`; the helpers page lists and assigns with `scope: "helpers"`) | Frontend `src/panels/config/helpers/ha-config-helpers.ts` |

Controls, all enforced by the plugin before anything is sent:

1. **Off until ticked, and needs an administrator.** `create_helper` is its own tier-write tool (the owner ticks it by name and should set it to Needs approval). Home Assistant refuses the command unless the token's user is an administrator; that refusal pauses admin calls for 60 seconds (shared with the automation definition tools) and says so.
2. **Prefix.** `helper_id` is 1 to 40 characters `a-z 0-9 _`, no leading, trailing or doubled underscore (so the slug is exactly the name). The helper's name is sent as `<helper_id_prefix><helper_id>` (setting `helper_id_prefix`, default `claude_`), so the entity is `input_boolean.claude_<helper_id>`. After creation the plugin checks that Home Assistant's id equals what was asked for.
3. **Category, required.** The owner names one existing helper category in `helper_category` (case-insensitive). Empty means the tool refuses. The category must already exist in Home Assistant (Settings, Devices and services, Helpers, Categories); the plugin checks it **before** creating anything and never creates categories. The new helper is put in that category with `config/entity_registry/update` (`{"categories": {"helpers": <id>}}`), nothing else about the entity registry is changed.
4. **Order of checks** (each refusal is before anything is sent): the setting is set; the id is well formed; the new entity id passes the policy (denied domain, deny pattern, allow list: otherwise it would be created and Claude could not see it; the rule is named); no entity with that id exists in Home Assistant, hidden or not (one answer: "already exists or cannot be created"); the category exists; the write-rate limit (shared with `call_service`). Then one WebSocket session: authenticate, create, wait up to about 3 seconds for the entity to appear in the registry, assign the category, close. Never retried.
5. **`initial`** is a boolean, default off. No icon, no other fields.
6. **Creating a helper does not make it writable.** The owner adds `input_boolean.claude_*` (or the single id) to `write_entity_allowlist`, and the services to `allowed_services`; the result says whether the new entity matches the write list now. This keeps "what exists" and "what may be changed" separate (section 5a).
7. **Partial outcomes are said plainly.** If the helper is created but the entity does not appear in time, or the category cannot be assigned (refused, or the id differs from what was asked), the result says what exists and what the owner must do in Home Assistant; nothing is retried or undone. A timeout says it may or may not have happened.
8. Audit records argument names only; the plugin counts `helpers_created` in the host statistics. Home Assistant's own error text is never repeated.

Not built: other helper domains (`input_number`, `input_select`, timers...), icons, update, delete, creating categories, changing an existing helper's category.

Unverified: python-slugify's treatment of underscores is relied on only through the post-creation id check; the delay before the entity registry entry exists; the exact error code a non-administrator gets over the WebSocket (the plugin treats any refusal of the create command as the administrator case).

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

## 10d. Acceptance checklist for the definition tools (Alex, against the real Home Assistant)

In Home Assistant create a dedicated **administrator** user for this (not your own account), and a long-lived token for it. Mark that user **local access only**. Use that user's token as the plugin's `token` (the plugin form never shows it back). Keep `automation_id_prefix` at `claude_`, keep `allowed_services` and
`write_entity_allowlist` as already set (light services on `light.*`). Under Policy tick `save_automation`, `delete_automation` and set
**both to Needs approval**. Then:

1. "Show the definition of <one of your automations>." Expect its triggers and actions, or "No definition available." for one with a
   template or one that names a lock or another hidden entity.
2. "Create an automation claude_porch_test: when <a binary sensor> turns on, turn on light.inside_porch, wait 10 seconds, turn it off."
   Approve. Expect it created and **disabled**. In Home Assistant, open it (Settings, Automations) and check it matches. Enable it in the
   interface and test it once.
3. "Add brightness 50 to the turn-on step." Expect an update that keeps your enabled/disabled choice.
4. "Create an automation that unlocks the front door at 7am." Expect a refusal. "Create one with a template." Expect a refusal.
5. "Change automation 1001" (or an automation you wrote) and "delete <one of yours>." Expect refusals that say the id must start with `claude_`.
6. "Delete claude_porch_test." Approve. Expect it gone from Home Assistant.
7. Open `automations.yaml` and check the other automations are intact (comments are not preserved: a known Home Assistant behaviour).
8. In Home Assistant open the user's profile and check the token's last use. Try the same token from a device outside your network (or
   through Nabu Casa) and confirm Home Assistant refuses it because the user is local only.

## 10e. If settings do not seem to take effect

Found during the first test of 0.4.0 (9 November 2026): the connector kept reporting "0 service(s) allowed" after the owner changed
`allowed_services`. The host keeps the **old** plugin version running when a reload of the new settings fails, so a rejected setting looks
like it was ignored. Check, in order: the plugin page shows no error after Save and reload; every list setting is valid (from 0.4.2 a pasted comma- or space-separated line is split into separate entries; before that it
was refused, naming the setting, which is how this was found); and `status` shows a recent "settings were loaded" time (added in 0.4.1). Write and admin tools such as
`delete_automation` are also only served after the owner turns read-only off and ticks each by name under Policy and saves the policy.

## 10f. Second live run of the definition and automation tools (corrected, real values)

Written 10 November 2026 after the first definition run (section 11). Every entity below was looked up through the connector that day. The lesson from earlier
runs applies: each refusal case must change exactly **one** thing from a valid definition, and every other part must be allowed, or the plugin
refuses it for an earlier reason and the rule under test is never reached. The checks run in this order inside `build_config`:
id prefix, alias/description/mode, list sizes, trigger types and fields, trigger entities, action service (against `allowed_services`), action entity
(visible and exists), service domain, write list, templates in data, target keys and other entities in data, self-trigger, "needs a service action",
and last the conditions. The name-clash check comes after all of those.

**Settings to enter first (owner).** On 10 November `status` showed `call_service` with 0 services allowed and no write patterns, whereas earlier that day it allowed 7 on
`light.*`. Whether that was intended is an open question (see below). For this run enter, one per line: `allowed_services` = `light.turn_on`, `light.turn_off`;
`write_entity_allowlist` = `light.outside_porch`, `light.inside_porch` (an explicit list rather than `light.*`, which would include `light.nursery_light`);
`automation_categories` = `claude`. In Home Assistant make sure a category named **Claude** exists (Settings, Automations, Categories) and put **no** existing
automation in it (all eight are in Car, System, Hoover or Lights today). Under Policy turn read-only off, tick `save_automation`, `delete_automation`,
`trigger_automation`, `set_automation_enabled`, and set all four to Needs approval. Run it in daytime: the test automation switches the outside porch light on for
10 seconds, and that light is off now, so it ends as it began.

Real values used: trigger sensor `binary_sensor.ev6_back_left_door` (a door sensor, off); light it acts on `light.outside_porch`; a visible light not on the write list
`light.lights_red_light`; a visible entity in the wrong domain `switch.ev6_charge_port`; a hidden entity `person.vicki` (person domain denied); a real
automation name for the clash test `Inside porch light - day, evening, night`; a real automation outside the category `automation.hoover_at_11am_when_house_is_quiet`.

### Prompt A (paste into claude.ai with the connector on)

```
Test the homeassistant connector. Use only the values written here. For every step report: the step, what you called, the reply (verbatim for refusals), and PASS or FAIL against "Expect". Do not skip a step, do not stop at a failure, and never retry a refusal with different values.

PART 1, read only
1. status. Expect: version 0.5.0, area information available, call_service "2 service(s) allowed" and 2 entity patterns, categories claude.
2. list_services. Expect: light.turn_on and light.turn_off, targets light.outside_porch and light.inside_porch.
3. list_automations. Expect: 8 automations, none starting claude_, all "controllable: no".
4. get_automation_definition for automation.inside_porch_light_day_evening_night. Expect either a definition or "No definition available." Report which; no pass or fail.

PART 2, refusals. Each uses save_automation unless stated, and must be refused. For all of them except where I say otherwise, the base definition is: automation_id claude_refusal_test, alias "Claude refusal test", triggers [{"type":"state","entity_id":"binary_sensor.ev6_back_left_door","to":"on"}], actions [{"service":"light.turn_on","entity_id":"light.outside_porch"}]. Change ONLY what is stated.
5. automation_id "porch_refusal_test". Expect: must start with claude_.
6. delete_automation with automation_id "inside_porch_light_day_evening_night". Expect: the same claude_ prefix refusal (not "No such automation.").
7. action service "light.toggle". Expect: not on the owner's allowed_services list.
8. action service "lock.unlock" with entity_id "lock.front_door". Expect: the same allowed_services message as step 7.
9. action entity_id "person.vicki". Expect: "cannot be used: its domain (person) is in the domain_denylist setting" (0.4.4; before that "does not exist or is not available").
10. action entity_id "light.does_not_exist". Expect: "does not exist in Home Assistant" (a different message from step 9, because this id passes the policy).
11. action entity_id "light.lights_red_light". Expect: "not on the write list".
12. action service "light.turn_on" with entity_id "switch.ev6_charge_port". Expect: must be in the same domain as the service.
13. action data {"brightness":"{{ 50 }}"}. Expect: template refused.
14. action data {"transition":"light.inside_porch"}. Expect: data may not refer to other entities.
15. action data {"area_id":"lights"}. Expect: data may not name targets.
16. trigger entity_id "light.outside_porch" (the same light the action changes). Expect: could trigger itself.
17. trigger {"type":"event","event_type":"call_service"}. Expect: each trigger needs a type (state, numeric_state, time, time_pattern, sun).
18. actions [{"delay":"02:00:00"},{"service":"light.turn_on","entity_id":"light.outside_porch"}]. Expect: delay longer than allowed.
19. conditions [{"type":"template","value_template":"{{ true }}"}]. Expect: each condition needs a type.
20. automation_id claude_refusal_name, alias "Inside porch light - day, evening, night". Expect: an automation with that name already exists.
21. list_automations. Expect: still 8 automations and none starting claude_ (nothing from steps 5 to 20 was created).

PART 3, create and read back
22. save_automation: automation_id claude_porch_test, alias "Claude porch test", description "Live test, safe to delete", mode single, triggers [{"type":"state","entity_id":"binary_sensor.ev6_back_left_door","to":"on"}], actions [{"service":"light.turn_on","entity_id":"light.outside_porch"},{"delay":"00:00:10"},{"service":"light.turn_off","entity_id":"light.outside_porch"}]. Expect: Created, entity automation.claude_porch_test, starts DISABLED.
23. get_automation_definition automation.claude_porch_test. Expect: the same triggers and the three actions, config id claude_porch_test, initial_state false.
24. list_automations. Expect: 9 automations; automation.claude_porch_test "controllable: no" (it has no category yet).
Then STOP and tell me: "In Home Assistant, put Claude porch test in the category Claude (Settings, Automations, select it, Add to category). Do NOT enable it. Reply done." Wait for me to reply "done".

PART 4, after I say done (the connector caches category data for 60 seconds, so wait a full minute after my reply before step 25)
25. list_automations with category "Claude". Expect: exactly automation.claude_porch_test, "controllable: yes". Then list_automations without a filter: the other 8 all "controllable: no".
26. save_automation again with the same id and the same definition, but the first action gets data {"brightness":128}. Expect: Updated; initial_state kept as the owner set it. Then list_automations with category "Claude" again. Expect: it is still there and still "controllable: yes" (the update did not drop its category).
27. set_automation_enabled automation.claude_porch_test enabled true. Expect: after state "on".
28. trigger_automation automation.claude_porch_test. Approve. Wait 15 seconds, then get_entity light.outside_porch and get_logbook for light.outside_porch for the last hour. Expect: the light turned on and, about 10 seconds later, off (it ends off); the logbook shows those two changes; no output contains any field starting "context_".
29. save_automation again, same as step 26, adding conditions [{"type":"state","entity_id":"binary_sensor.ev6_back_left_door","state":"on"}] (the door is closed, so the condition is false). Then trigger_automation automation.claude_porch_test. Wait 15 seconds, then get_history of light.outside_porch for the last hour. Expect: no new on/off change after the trigger (the condition was respected).
30. set_automation_enabled automation.claude_porch_test enabled false. Expect: after state "off".

PART 5, the fence
31. save_automation: automation_id claude_uncategorised_test, alias "Claude uncategorised test", the same triggers and three actions as step 22. Expect: Created, DISABLED. It stays in NO category.
32. list_automations. Expect: automation.claude_uncategorised_test "controllable: no" even though its id starts with claude_.
33. trigger_automation automation.claude_uncategorised_test. Expect: "may not control that automation".
34. set_automation_enabled automation.claude_uncategorised_test enabled true. Expect: "may not control that automation".
35. set_automation_enabled automation.hoover_at_11am_when_house_is_quiet enabled true (it is already on, so nothing changes even if this were wrongly allowed). Expect: "may not control that automation".
36. trigger_automation automation.does_not_exist. Expect: "No such entity."
37. set_automation_enabled with entity_id light.outside_porch enabled true. Expect: must be an automation.
38. get_entity automation.claude_uncategorised_test. Expect: state off and last_triggered empty (it never ran).

PART 6, clean-up
39. delete_automation claude_porch_test, then delete_automation claude_uncategorised_test. Approve both. Expect: both deleted.
40. list_automations. Expect: the original 8 only. get_entity light.outside_porch: off.
Finish with a table of all 40 steps and a list of anything that surprised you. Do not call any tool not named above.
```

What each part proves, and what it does not: part 2 reaches one rule per case (steps 5 to 20 are each isolated by construction); part 4 is the first live use of
`trigger_automation` and `set_automation_enabled`, with the category fence and with conditions respected (step 29); the 60 second wait before step 25 is real: the registry cache (`REGISTRY_TTL`) would otherwise show the old category. The refusal cases of part 2 and the fence cases of part 5 are replayed on every test run by `tests/test_homeassistant_live_prompt.py` against a fake that mirrors this home, so the prompt cannot drift from the code. Part 5 confirms that a `claude_` id is **not** itself
permission to run an automation: the category is the control (section 9b). If steps 33 to 35 are not refused, stop and report: that is a defect in the fence.

### Prompt B (entity pattern rule; separate run, because it changes settings)

Before: note the counts from `list_areas` for Lights and Car, and the number of results of `search_entities` with domain `binary_sensor` and query `EV6 Back Right`. Then set
`entity_denylist` = `light.lights_red_light` and `binary_sensor.ev6_back_right_*` (one per line), save and reload, and paste:

```
Test the homeassistant connector's entity patterns. Report each step with PASS or FAIL against "Expect", and do not call any tool not named.
1. get_entity light.lights_red_light, then get_entity sensor.this_does_not_exist. Expect: both reply exactly "No such entity."
2. get_states for light.lights_red_light, light.outside_porch. Expect: red light "not found", outside porch listed.
3. search_entities query "Red Light". Expect: no Red Light in the results.
4. search_entities domain binary_sensor query "EV6 Back Right". Expect: no results (it was non-zero before).
5. get_entity binary_sensor.ev6_back_left_door. Expect: works (the glob only matches back_right).
6. get_history of light.lights_red_light for 1 hour, and get_logbook of binary_sensor.ev6_back_right_door for 1 hour. Expect: both answer "No history found" / "No logbook entries" in the same words as for a missing entity.
7. list_areas. Expect: Lights has one fewer entity than before, Car has fewer by the number of back-right sensors found before.
```

Then remove both patterns, reload, and confirm step 1 now returns the red light. Not part of either prompt: a revoked-token check (the owner revokes the token in Home Assistant; `status` should show the refusal,
then a 60 second pause) and `entity_allowlist`; both are still unverified live.

### Open questions for the owner

1. The settings difference on 10 November (7 services on `light.*` earlier in the day, 0 services and no patterns after the update): was that intended, or did the update reset them?
2. Should the write list be the explicit list above instead of `light.*`, so the nursery light can never be a target? It is currently `unavailable` in Home Assistant.

## 10g. Acceptance checklist for `create_helper` (Alex, against the real Home Assistant)

Looked up on 11 November 2026 through the connector: Home Assistant 2026.11.1; `input_boolean` has three entities (`car_30`, `car_60`, `car_now`) and no
`doorbell_chime`; `event.doorbell_doorbell` exists with state `unknown` (an event entity before its first event, as the source says).

**Set up (owner).** Update the plugin to 0.5.0. In Home Assistant create a **helper category** (Settings, Devices and services, Helpers, then the category filter, Add category) named
`Claude`. In the plugin settings set `helper_category` = `Claude`; leave `helper_id_prefix` at `claude_`; set `write_entity_allowlist` = `input_boolean.claude_doorbell_chime` (separate from
`entity_allowlist`, which stays empty) and keep `allowed_services` = `input_boolean.turn_on`, `input_boolean.turn_off`. Under Policy tick `create_helper` and `save_automation` and set
both to Needs approval. The token's user must be an administrator (as for the definition tools).

```
Test create_helper on the homeassistant connector. For each step report the step, the call, the reply (verbatim for refusals) and PASS or FAIL against "Expect". Do not retry a refusal with other values and do not call any tool not named.
1. status. Expect: version 0.5.0 and a line "Helpers Claude may create: input_boolean helpers named input_boolean.claude_<name>, in the helper category Claude". Also: input_boolean has 3 entities.
2. create_helper with helper_id "door__chime". Expect: refused, helper_id is 1 to 40 lower-case letters, digits and single underscores.
3. create_helper with helper_id "Doorbell Chime" (with a space). Expect: the same refusal.
4. search_entities with domain input_boolean. Expect: still exactly 3 (car_30, car_60, car_now): nothing was created by steps 2 and 3.
5. create_helper with helper_id "doorbell_chime". Approve. Expect: "Created the helper input_boolean.claude_doorbell_chime (off to start)", "It is in the helper category Claude", and that it matches write_entity_allowlist.
6. get_entity input_boolean.claude_doorbell_chime. Expect: state off.
7. create_helper with helper_id "doorbell_chime" again. Expect: "already exists, or cannot be created" and nothing new.
8. search_entities with domain input_boolean. Expect: exactly 4, the new one included.
9. status. Expect: "1 visible entity(ies) match those patterns" and no "Write note".
10. save_automation: automation_id claude_doorbell_chime, alias "Doorbell chime", triggers [{"type":"state","entity_id":"event.doorbell_doorbell"}], actions [{"service":"input_boolean.turn_on","entity_id":"input_boolean.claude_doorbell_chime"},{"delay":"00:00:03"},{"service":"input_boolean.turn_off","entity_id":"input_boolean.claude_doorbell_chime"}]. Approve. Expect: Created, starts DISABLED.
Finish with a table of the 10 steps.
```

Then, in Home Assistant, check by hand: the Helpers page shows `claude_doorbell_chime` as a toggle, in the category Claude, off; the new automation exists and is off. There is no delete tool: remove the helper
and the automation in Home Assistant when you have finished (or keep them: the chime is the point). To enable the automation, turn it on in Home Assistant and press the doorbell once; the helper should switch on for
3 seconds. Optional: set `helper_category` to a name that does not exist and repeat step 5: expect "No helper category ... nothing was created". Optional: use a non-administrator token and repeat step 5: expect the
"refused the helper command" message, then a pause message on the next call. Not covered by this run: HA's slug rule for ids with several underscores (the plugin checks the id it gets back), and a slow registry.

## 11. Live check results

10 November 2026, plugin 0.4.x on Alex's host, definition tools, by the 12-step prompt: steps 1 to 7 and 9 to 11 passed (read a definition, create, read back, update, delete, the prefix rule, clean-up).
Step 8 (ten refusal cases) proved only seven rules: a lock service, `light.toggle` and a switch entity were refused by the service allow-list or the write list before they reached the rule under
test (the prompt used values that were not allowed). Step 12 (`trigger_automation` and `set_automation_enabled` cannot reach an automation outside the allowed category) was not confirmed: no category data was
available then. Later that day the host installed `wsproto` and `list_areas` returned 16 areas. Section 10f repeats both with real values.

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

