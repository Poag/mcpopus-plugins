# homeassistant: design

Written 6 November 2026. Specification of the `homeassistant` plugin (endpoint `/homeassistant`), version 0.1.0, **read-only**. Conventions
and the rules every plugin follows are in [DESIGN.md](DESIGN.md) section 1; the host's plugin contract is in the mcpopus repository
(`docs/PLUGINS.md`, `docs/SECURITY.md`). Status: built and tested against a fake Home Assistant only. Nothing here has run against a real
Home Assistant: the live check is in section 10.

## 1. Goal

Let Claude read the state of Alex's home: which entities exist, what state they are in, which area they are in, what they did recently.
A later phase adds a small, explicitly allowed set of changes (section 9). Policy about what Claude may see is enforced by the plugin,
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

All are tier `read`. All return text that came from Home Assistant, so all except `status` are `untrusted=True` (the host wraps them
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
| `list_services` | `domain` | The services in `allowed_services` with their descriptions. Empty by default. Nothing can call them in this version |

Not built, on purpose: the template endpoint, a raw API pass-through, any call to an arbitrary URL, the camera proxy, the error log,
event firing, state setting, WebSocket subscriptions.

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
| `allowed_services` | empty | `domain.service` pairs, exact; refused at load if the domain is in `domain_denylist`. Used by write tools in phase 2 |
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

## 9. Writes (phase 2, not built)

Planned, to be built only after Alex confirms the read-only checklist and names the services and entities:

- One tool, `call_service`, tier `write`, off until ticked by name in the host policy. It calls only `domain.service` pairs listed in
  `allowed_services`, on entities the entity policy allows. A service that is not listed is refused with the same message as an
  unknown service.
- The target is one named entity (`entity_id`, a string). The service data may not contain `entity_id`, `area_id`, `device_id`,
  `label_id` or `floor_id`, so "all", area-wide and device-wide targets are impossible unless the allowlist says so explicitly.
- It returns the entity's state before and after, and lists changed states only for entities the policy allows (Home Assistant returns
  every state changed under the call's context, which can include hidden entities).
- Its own rate limit (calls per minute), on top of the host's. `return_response` is not supported in the first write version.
- Decisions needed from Alex first: which entities, which services, and whether `cover`, `climate` and `switch` may be written at all.

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

