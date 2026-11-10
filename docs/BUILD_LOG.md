# Build log (plugins)

Dated entries. "Verified" means the source named was read on the date given. Anything not verified is under "Unverified" and is written
so that it is easy to change. The host's own log is `docs/BUILD_LOG.md` in the mcpopus repository.

## 2026-11-10: homeassistant hand-over, corrected live checklist (no plugin change, still 0.4.3)

Took over the plugin. CI green on `main` (bb0f98a, run 33). 71 homeassistant and requirements tests pass locally against host 0.3.0. No behaviour change, so no version bump.

### Found, through the connector (read tools only)

- `status`: Home Assistant 2026.11.0, plugin 0.4.3, area information available, 674 visible entities. `call_service` allows **0 services and 0 patterns**; `automation_categories` is `claude`.
- `list_automations`: 8 automations, **none in a category called Claude** (they are in Car, System, Hoover and Lights), so none is controllable. Nothing named `claude_` exists, so the 10 Nov clean-up held.
- `list_areas`: 16 areas. Real entities for the tests were looked up (design section 10f): `binary_sensor.ev6_back_left_door`, `light.outside_porch`, `light.inside_porch`, `light.lights_red_light`, `light.nursery_light` (unavailable), `switch.ev6_charge_port`.
- Reading the code showed why step 8 failed to test three rules: `build_config` checks the service against `allowed_services` first, then the entity, then the domain, then the write list. A case only reaches a later rule if everything earlier is allowed. Step 12 had also been framed wrongly: the category, not the `claude_` prefix, is what lets an automation be triggered or switched (design 9b), so a `claude_` automation outside the category must be refused.
- The registry (areas, categories) is cached for 60 seconds, so a category assigned in Home Assistant can show as "controllable: no" for a minute.

### Added

- Design section 10f: settings to enter, real values, **Prompt A** (40 steps: read-only checks, 16 isolated refusal cases, create and read back, a pause for the owner to put one test automation in the Claude category, update, enable, trigger, a false condition, the fence on an uncategorised `claude_` automation, clean-up) and **Prompt B** (entity deny patterns on a real light and a real glob). Section 11 now records the 10 Nov results.
- `tests/test_homeassistant_live_prompt.py` (3 tests): replays the prompt's refusal and fence cases against a fake that mirrors this home, with the settings the checklist asks for. It asserts the message each case must reach, that the base definition and the later valid definitions are accepted (otherwise every refusal could pass for the wrong reason), and that nothing reaches Home Assistant.
- Mutation checks, each made a test fail and was restored: write list ignored; service allow-list ignored; domain match ignored; template in data allowed; self-trigger allowed; delay cap removed; prefix fence loosened; category fence opened; name clash ignored. Removing only the `visible()` check in `Context.entity` is survived, because `save_automation` already passes only visible states (two layers); removing both makes the test fail.

### Not verified

Everything in Prompt A and B against the real Home Assistant (waits for the owner). The revoked-token check and `entity_allowlist` live. Whether a disabled automation can be triggered by `automation.trigger` (the prompt enables it first). Whether an update through `save_automation` keeps the automation's category (a real-data check in step 26).

### Open for the owner

1. The 10 Nov settings change (7 services on `light.*` earlier, 0 after the update): intended or reset?
2. Use an explicit write list (outside and inside porch) instead of `light.*`, so the nursery light can never be a target?

## 2026-11-10: obsidian 0.5.1 (first live run of query_base: `file.name` has no extension)

Live run on the owner's vault with plugin 0.5.0 (read-only, plus one fixture note `Test/zzqx.md`, created and deleted). Results: `Index.base` (no filters) listed 307 notes, equal to the readable notes, with 6 other files counted (4 bases + 3 yaml minus the blocked base), so the blocked folder is in no count; `Search.base` was refused by name for `file.backlinks` until the owner trimmed its columns, then ran (0 notes, header correct); `Large.base` refused as over `max_base_bytes`. The `zzqx` check: the plugin said 0 notes, the owner's Obsidian listed `zzqx.md`, so **`file.name` excludes the extension**; section 11.4's first choice was wrong. Fix: `file_value` returns the basename for `file.name` (filters, columns and sort); tests changed to match, and putting the old behaviour back fails two tests (mutation check). Suite: 335 passed, 10 skipped (local). Not verified: `file.name` for non-note files (same rule used), checklist items 2 to 4 (`inFolder`, property and tag filters, `mtime`), a direct query of the Medical base (declined by the session's classifier; blocked equals missing is shown by the counts instead).

## 2026-11-10: obsidian 0.5.0 (Bases phase 2: query_base, read-only)

Built after the owner's go-ahead. Design first: DESIGN-bases.md section 11 (subset, chosen meanings where the documentation is silent, refusals, live checklist).
`basequery.py`: a small tokenizer, parser and evaluator (no `eval`; length, node, depth and step budgets), the filter groups, the supported file and note
properties and functions, date arithmetic in UTC, table views with `order`, `sort` and `limit`, and refusal by name of everything else (formulas, `this`, unlisted functions,
`groupBy`, `groupOrder`, view `summaries`, other view types, any other view or top-level key). `vault.query_records` builds the rows over the policy-filtered view and
also the metadata of non-note files, which are counted and never listed. `query_base(path, view, limit, offset)`; the answer states that it is computed by mcpopus and may differ from Obsidian.
Tests: `tests/test_query_base.py` (125): the evaluator with a fixed clock, the refusal tables, and the tool through the real host manager and fake CouchDB (policy leakage
including a mutation check, hostile property text, no YAML library, scan bounds, unreadable notes, no writes). Further mutation checks (six edits, each made a test fail):
`not` as not-all, unknown view keys accepted, blocked other files counted, hidden folders counted, type problems silently false, a path-only test resetting the needs-contents flag.
Two defects found by the tests and fixed before commit: `file.inFolder` was treated as needing note contents; the depth counter charged every operator level.
Suite: 335 passed, 10 skipped. **Not run on the real vault.** Unverified against Obsidian (section 11.4): whether `file.name` includes the extension in the owner's Obsidian, equality and
missing-value rules, default sort, case rules, `today()` and time zones, link and tag matching. CI for this commit not yet seen.

## 2026-11-10: plugins declare their packages (host 0.3.0): homeassistant 0.4.3, swarmui 0.1.1, obsidian 0.4.2

Host 0.3.0 (read from its `docs/PLUGINS.md` "Dependencies" and `src/mcpopus/packages.py` at `origin/main` 1ddb64d) carries only what it needs and installs
exact-pinned wheels listed in a plugin's `requirements`. Scan of every `.py` file's imports (AST, including `find_spec` names): `email` imports nothing
outside the standard library and the host's list; `homeassistant` and `swarmui` use `wsproto` only through httpx2's WebSocket client; `obsidian` imports
`yaml` (the Bases parser from 0.4.0, which the host image never had).
Now listed, all with `--hash=sha256:` (wsproto and h11 are single pure wheels; PyYAML lists the two cp313 manylinux wheels, x86_64 and aarch64, the host being
`python:3.13-slim`): homeassistant and swarmui `wsproto==1.3.2`, `h11==0.16.0` (wsproto's dependency; hashed mode needs it listed, and it equals the host's version);
obsidian `PyYAML==6.0.3`. All three set `min_host_version = "0.3.0"`. `email` unchanged. "Library missing" messages now say "install the plugin's packages from its page".
Tests: `tests/test_requirements.py` (12): every manifest passes `mcpopus.packages.parse_requirements`; an import outside the host's list or the plugin's own requirements
fails (mutation-checked: removing PyYAML, a range instead of a pin, and an old `min_host_version` each fail); the pins match the test environment; the host refuses a plugin
whose packages are not installed. The harness gives the manager a `Packages` object and records the requirements as installed (the test environment already has them).
Suite: 210 passed, 10 skipped, against the real host 0.3.0.
Verified once by hand here: the host's own `Packages.install` (pip, `--require-hashes`, wheels only) installed exactly these pins from PyPI for all three plugins on
linux x86_64, Python 3.13. Not verified here: the aarch64 PyYAML hash (taken from PyPI's file list, not installed), and loading from the installed directory (the test venv already
has the packages and host-provided packages win).
**Confirmed on the owner's host (host 0.3.0, 10 Nov 2026, 10:00 UTC, from its log and the connector):** the host installed `wsproto 1.3.2` and `h11 0.16.0` for homeassistant and swarmui, and
`pyyaml 6.0.3` for obsidian, in hashed mode, in 3.5 to 3.9 s each, and loaded homeassistant 0.4.3, obsidian 0.4.2 and swarmui 0.1.1. Then, through the connectors:
`read_base` on a real base returns `parse: ok` with the view summary; Home Assistant `status` says "Area information: available" and `list_areas` returns 16 areas.
The host's CPU architecture was not stated, so which PyYAML hash it used is not known. SwarmUI live progress was not checked. CI for the commits: green.
Owner: on the Plugins page fetch and apply the three updates (tick the trust box; the host installs the packages), then reconnect the connector in claude.ai.
Also noted: host 0.2.0 refuses undeclared tool arguments, which settles the earlier open question about unknown arguments.

## 2026-11-10: obsidian 0.4.1 (first live run of Bases phase 1)

Owner's read-only run on the real vault. Confirmed: `.base` is stored as `newnote` (2 of 2); a blocked base, a missing base and a base in a missing
folder answer alike; path rules; counts. Found: `list_bases` said "showing 0 to 2" for an offset past the end; fixed with a test that fails on the old code.
Four steps of my test prompt were wrong (write tools are allowed by the owner's policy; one readable base cannot show paging; the embedding note was empty;
the search word occurred in a note). Link resolution is still untested live. Details: DESIGN-bases.md section 10. CI for this commit not yet seen.

## 2026-11-09: obsidian 0.4.0 (Bases phase 1, read-only)

Brief: let Claude see and maintain Obsidian Bases (`.base`), in three phases with stops. This is phase 1. Design: [DESIGN-bases.md](DESIGN-bases.md).

Verified by reading source on this date (details and sources in the design doc, section 1): the Bases file format and function reference
(obsidianmd/obsidian-help); that LiveSync's `isPlainText` list does not include `.base`, so a base is stored as a `newnote` document with
base64 chunks (livesync-commonlib at `6b59815` and `main` 0.4.1, obsidian-livesync `main`, octagonal-wheels `main`); that the existing reader
already received such documents and silently dropped them because the path did not end in `.md`; that `get_note_metadata` has no notion of a
link resolving, so any "broken link" report about a base comes from outside this repository.

Built: `bases.py` (safe YAML parsing: PyYAML SafeLoader, anchors and aliases refused, node and depth caps, object-constructing tags refused, one
document, mapping at the top; summary; link extraction), a separate base index in `vault.py`, `list_bases`, `read_base`, base counts and
extension counts in `vault_status`, a `bases:` line in `get_note_metadata`, setting `max_base_bytes`. 17 new tests (suite 198 passed, 10
skipped), including a mutation check that the policy leakage assertions fail when the policy is removed, and a side-effect check that an
object-constructing YAML tag does nothing.

Unverified: everything against a real LiveSync database; the view `sort` key and view-type keys; how the owner's audit reports base links;
whether the owner's vault stores `.base` as `newnote` (the live checklist in the design doc answers it); CI for this commit.

Needs from the host: PyYAML pinned in `pyproject.toml` and `requirements.lock` (not present today). Until then `read_base` returns the raw text with `parse: unavailable`.

## 2026-11-09: homeassistant 0.4.2 (comma-separated lists accepted)

The owner's `allowed_services` screenshot showed the cause of the unapplied settings: seven services on ONE comma-separated line, refused by the
validator, so the reload failed and the host kept the old settings. My own instructions had listed them with commas, which invited it.
List settings (allowed_services, the entity lists, domain_denylist, redact_attributes) now split pasted commas, semicolons and spaces into
entries. Category names are not split (they may contain spaces). A wildcard in a service is still refused. Test added.

## 2026-11-09: homeassistant 0.4.1 (status shows plugin version and settings load time)

First live test of 0.4.0: all refusals worked, but the owner's new `allowed_services` and `write_entity_allowlist` never reached the connector
(status kept saying 0 services) and `delete_automation` was not served. Not a plugin fault as far as can be seen: the host keeps the old
version running when a reload fails (for example a comma-separated list, which the validator rejects), and admin tools need their own tick.
Lesson: the plugin could not show whether a reload had applied. `status` now reports the plugin version, when the current settings were
loaded, and a hint. Design section 10e records the checks. Also found: the test prompt's placeholder entity made most step 8 refusals test
the wrong rule, so prompts must use real entities.

## 2026-11-09: homeassistant 0.4.0 (automation definitions, ported from ha-mcp's approach)

Alex asked whether `homeassistant-ai/ha-mcp` could be reused as a plugin and chose "port chosen capabilities" over a proxy or a separate
connector. Researched its README, `pyproject.toml`, `SECURITY.md` and automation tool source (MIT, Python 3.13 to 3.14, 87 tools, expects an
administrator token, trusts MCP clients). Built the automation definition tools into this plugin under its own policy model; ha-mcp is
credited in NOTICE.md for behaviours learned (reload lag and entity replacement, alias collision guard, config API, `initial_state` override).
No code copied.

Decisions (revised the same day): the first build had a separate optional `admin_token`; Alex asked to reuse the one `token`, so it was removed. A
non-administrator token now only loses the three definition tools (a refused config call pauses just those for 60 seconds, not the whole
plugin). Alex's mitigation for the larger blast radius: a dedicated administrator user marked "local access only", which Home Assistant
enforces by refusing that user's token from non-local addresses and over Nabu Casa (verified in Core `components/http/auth_util.py`).
Other decisions: id-prefix fence (`claude_`) so human automations are
never changed; a whitelist builder instead of passing YAML through; templates refused everywhere; new automations start disabled; defaults
taken from the 9c proposal because Alex's choice of option B and his earlier request for all four automation features pointed that way.

Verified: Home Assistant accepts `trigger:` and `platform:` for trigger types and `action:` for service calls; `initial_state` is a valid key.
Tests: 16 new (fake Home Assistant with a separate administrator token and a config API); the fake refuses a non-administrator on the config API, and
tests assert one refusal then a pause, and that everything else keeps working. Mutation checks: the prefix fence, the template rule in action data, the write
list, the service allow list, the self-trigger guard, starting disabled, the hidden-definition filter, sending the admin token everywhere,
and the alias guard each make at least one test fail. The template-in-data test found a real gap (templates in action `data` were accepted)
which is fixed.

Unverified: everything against the real Home Assistant (section 10d); older Home Assistant field names; whether a config write triggers a
reload that restores a previously enabled state.

## 2026-11-08: swarmui 0.1.0 (built: read tools, generation, optional WebSocket progress)

Alex asked to build all phases and test at the end. Built to the plan in `docs/DESIGN-swarmui.md` (section 14 lists what was built and what
was not): HTTP background jobs, least-privilege Swarm user as the main control, prompt tag syntax refused by default (LoRAs become
validated tags), size, rate, queue and concurrency limits, model allow and deny lists, an allow list for extra settings with image
inputs and output locations never allowed, image paths filtered, SwarmUI's own error text never repeated, interrupt scoped to this
connector's session. WebSocket progress only when `wsproto` exists, never as a silent second generation. Images are not returned to
Claude (host limitation). 33 new tests, all protections mutation-checked.

Lessons: the first WebSocket error path swallowed the real error because the client wraps failures in exception groups; both WebSocket
readers (swarmui and the homeassistant registry reader) now classify the first real exception, and the homeassistant one now reports a
refused token as such. A test that checked "prompts are not logged" failed because the test client's own debug output logged the
request, so it now looks at the plugin's loggers only.

Unverified: everything against a real SwarmUI; parameter ids and the LoRA parameter for the release in use; queue behaviour; the job
timeout path (untested, 30 seconds minimum).

## 2026-11-08: swarmui plan (design only)

Alex asked for a plan for a SwarmUI plugin. Written as `docs/DESIGN-swarmui.md`; no code. Verified from the SwarmUI repository's own API
and permission documentation: sessions and `swarm_token` accounts, per-route permission flags (so a least-privilege Swarm user is the main
control), the blocking `GenerateText2Image` route and the WebSocket variant, the parameter and model listing routes, and the prompt tag
syntax, which can load models, read server wildcard files and set any parameter (so prompt syntax is refused by default).
Host findings: tool results are text only, so Claude cannot see generated images (smallest host change written down, not assumed);
WebSocket progress would need `wsproto`, so version 1 is HTTP only with a background job. Unverified: everything against a real
SwarmUI, the parameter ids of the release in use, LoRA handling, queue behaviour. Phases: read-only, then generation, then optional.

## 2026-11-08: homeassistant 0.3.0 (automations)

Alex asked to integrate with automations: list and inspect, trigger, enable and disable, read definitions, and create, edit or delete, with
control limited to "those in a specified category". Built: the first three (the last two designed in section 9c, not built).

### Verified (Home Assistant core source, `dev` branch, 8 November 2026)

- Automation services and their field defaults; `skip_condition` defaults to true (conditions skipped). The plugin always sends false.
- Category registry list command and the `categories` field on entity-registry entries (read-only WebSocket, same as areas).
- The automation config API is administrator-only, replaces a whole automation, rewrites `automations.yaml`, reloads, and on delete
  also removes the registry entry. A category cannot be set through it.

### Decisions

- The category fence needs registry data over WebSocket, so the automation tools depend on `wsproto` in the host image, the same
  host change areas need. Without it they fail closed and say why. A write-pattern (`write_entity_allowlist`) fallback exists.
- Dedicated tools instead of letting `call_service` take automation services: each gets its own host tick, always respects conditions,
  and cannot be used to bypass the category rule. `automation.*` in `allowed_services` is refused at load.
- Reading and editing definitions is not built: it needs an administrator token, rewrites a YAML file, and a created automation runs
  outside the plugin's policy. Design and the open decisions are in section 9c.

### Verified by tests

Eleven new tests (fake Home Assistant, real host manager and client). Mutation checks: sending `skip_condition: true`, `stop_actions: true`,
ignoring the category fence, removing the hidden-entity check, and removing the rate limit each fail at least one test. Unverified: all
of it against the real Home Assistant (section 10c), and whether a disabled automation can still be triggered by the service.

## 2026-11-08: homeassistant 0.2.0 (call_service)

### Decisions

- Alex allowed seven services (light, switch, cover on/off or open/close, climate set_temperature) but only `light.*` as targets. A separate
  `write_entity_allowlist` (default empty) expresses that; the service list alone would have allowed cover and climate targets.
- Writes need three independent switches (host tick, service pair, entity pattern) and every refusal happens before a request is sent.
- A hidden entity and a missing one give the same answer even when the write list is involved: existence is checked against the visible
  states before the write list, so "not on the write list" is only ever said about an entity that is visible anyway.
- Targets cannot be widened: one `entity_id` string, target keys and foreign entity ids rejected in `data`, entity domain must equal the
  service domain.
- Changed states returned by Home Assistant are filtered by the read policy before being shown.
- The write tool is in the same plugin as the untrusted-content readers, as the brief specified; mitigations and the recommendation to
  set Needs approval are in the design, section 9.

### Verified

- Source of the REST service-call view (read on 6 November): `POST /api/services/<domain>/<service>`, JSON body is the service data,
  400 for validation or unknown service, 500 for Home Assistant errors, the reply lists changed states under the call's context.
- Eleven new tests (fake Home Assistant, real host manager and client) including a zero-write assertion at the fake when the tool is off.
  Mutation checks: ignoring the write list, the service allowlist, the target-key rule, the rate limit, the changed-state filter or the
  domain rule each makes at least one test fail.

### Unverified

Everything against the real Home Assistant (section 10b is the live check); the behaviour of slow devices whose state updates after the
call returns (the output tells the model to confirm with `get_entity`); services whose fields reference other entities by name.

## 2026-11-07: homeassistant 0.1.0, first live check

Ran the 18-step prompt against the real Home Assistant: no failures; area steps skipped (no `wsproto` in the host image). Details and gaps in
`docs/DESIGN-homeassistant.md` section 11. Lesson: a test prompt with unfilled placeholders produces a "pass" for the steps that need none and
silently stops on the rest, so the report said "not run" and had to be repeated with real values.

## 2026-11-06: homeassistant 0.1.0 (read-only)

### Verified

- Home Assistant developer documentation, read from `home-assistant/developers.home-assistant` on GitHub (`docs/api/rest.md`,
  `docs/api/websocket.md`, `docs/auth_api.md`): REST and WebSocket APIs, bearer token use, long-lived tokens valid for 10 years, the
  WebSocket auth handshake, the REST endpoint list (no areas, devices or entity registry), the template endpoint.
  `developers.home-assistant.io` itself could not be reached from the session's network, so the site was not read.
- Home Assistant core, `home-assistant/core` branch `dev` (version constant 2026.12.0.dev0, so unreleased): `components/api/__init__.py`,
  `components/history/__init__.py`, `components/logbook/rest_api.py`, `components/websocket_api/{auth,commands,const}.py`,
  `components/config/{area,device,entity}_registry.py`, `helpers/*_registry.py`, `components/http/ban.py`. Findings that shaped the
  design: the registry lists are read-only WebSocket commands open to non-administrators; the REST history view applies the user's read
  permissions and requires an entity filter; **the REST logbook view applies none and returns `context_*` fields naming other
  entities**; the services endpoint's shape differs between docs and source; a service call returns every state changed under its
  context; IP banning is off unless a threshold is configured.
- mcpopus host: `requirements.lock` has no WebSocket library. `httpx2` 2.16.0 has a WebSocket client that needs the optional
  `wsproto` package (read from the installed wheel). `wsproto` 1.3.2 is the newest release on PyPI (`pip index versions`).
- The host's plugin API (`PluginContext`, `ctx.stats`, per-plugin policy) is unchanged for this plugin's needs.

### Decisions

- REST for states, history, logbook and services; WebSocket only for the three registry lists. Reason in the design, section 2.
- The area features depend on a library the host image lacks. Not worked round with a hand-written WebSocket client: the plugin uses
  httpx2's own client, reports clearly when `wsproto` is missing, and the smallest host change is written down for Alex.
- The plugin builds its own HTTP client because the host's shared one cannot carry a custom certificate authority or a verify flag.
- A denied entity gets the same answer as a missing one by checking policy before any request, so Home Assistant is never asked about
  a denied entity and cannot be an oracle.
- Calls are paused for 60 seconds after a token refusal, so a revoked token cannot cause repeated failed logins.
- The logbook tool takes one entity, strips `context_*`, and drops entries that name a hidden entity id. It is a heuristic and the
  design says so.
- `docs/PLUGINS.md` and this log live in the plugins repository, because the host repository is read-only for this work.

### Lessons

- Read the implementation, not only the documentation: the logbook's missing permission filter and the services shape are in the
  source, not in the docs.
- A first draft of one "hidden entities are absent" test passed for the wrong reason (the output echoed the ids it was checking
  for). Mutation checks (policy forced open; logbook filters removed) now show the tests fail when the protections go.

### Unverified

Everything against a real Home Assistant; behaviour of the released version (the source read was a development branch); whether a
non-administrator user can be limited to certain entities inside Home Assistant; the behaviour when the recorder or logbook
integrations are off; reverse-proxy WebSocket forwarding. See the design doc, section 3.
