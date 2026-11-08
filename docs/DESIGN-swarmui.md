# swarmui: design (plan, nothing built)

Written 8 November 2026. Plan for a `swarmui` plugin (endpoint `/swarmui`) that lets Claude generate images with a SwarmUI instance
(Stable Diffusion and its successors) and look at what the instance has. Status: **design only**; no code, no tests, nothing run against
a real SwarmUI. Conventions and the rules every plugin follows are in [DESIGN.md](DESIGN.md) section 1; the host's plugin contract
is in the mcpopus repository.

## 1. Goal

Claude can ask for an image and be told when it is done, check what models and settings exist, and see what has been made, without
being able to change the SwarmUI server, its models, its users or other people's images. Generation spends GPU time and disk, so
limits on size and rate are part of the design, not an afterthought.

## 2. What was verified

All read on 8 November 2026 from the SwarmUI repository, `mcmonkeyprojects/SwarmUI`, default branch, documentation files `docs/API.md`,
`docs/APIRoutes/{T2IAPI,ModelsAPI,BasicAPIFeatures,BackendAPI,AdminAPI,UtilAPI}.md`, `docs/Sharing Your Swarm.md`,
`docs/Features/Prompt Syntax.md`, `docs/Image Metadata Format.md`. The version in the API example reply was 0.6.3.0; the documentation
tracks the development branch, so details may differ in the release Alex runs.

| Fact | Source |
| --- | --- |
| The API is `POST` JSON to `<server>/API/<Route>`; routes ending `WS` are WebSockets that stream progress. Default port 7801 | `docs/API.md` |
| Every route except `GetNewSession` needs a `session_id` in the JSON. `GetNewSession` returns `session_id`, `user_id`, `version` and the user's **`permissions`** list. An `error_id` of `invalid_session_id` means get a new session and retry | `docs/API.md`, `BasicAPIFeatures.md` |
| If the instance requires accounts, requests need a cookie named `swarm_token` holding a token made in the UI (User tab, Swarm Auth Tokens). Tokens belong to a user, and a user has a role | `docs/API.md`; `Sharing Your Swarm.md` |
| Every route is gated by a named permission flag. Generation needs `basic_image_generation`; listing images needs `view_image_history`; models need `fundamental_model_access`; status and parameter lists need `fundamental_generate_tab_access`. Dangerous routes have their own flags (`delete_models`, `download_models`, `load_models_now`, `add_remove_backends`, `edit_backends`, `restart_backends`, `edit_wildcards`, `user_delete_image`, `edit_user_settings`, ...) | route pages |
| Roles can hold any subset of permissions, and can white- or black-list model folders; the instance owner can create users and roles. The documentation says SwarmUI is only validated as safe for "Friends and Family", not for the public | `Sharing Your Swarm.md` |
| `GenerateText2Image` (HTTP) takes `images` (count) and `rawInput`, an object of parameter ids to values (`prompt`, `negativeprompt`, `model`, `width`, `height`, `steps`, `cfgscale`, `seed`, ...), and returns `{"images": ["View/<user>/raw/<date>/<file>.png"]}`; the call **blocks until the images exist**. The WebSocket variant `GenerateText2ImageWS` sends `status`, `gen_progress` (percent, optional preview) and `image` messages, and `discard_indices` | `T2IAPI.md`, `docs/API.md` |
| `ListT2IParams` lists every parameter with id, type, default, min, max and allowed values. Parameter ids are the lower-case name without spaces | `T2IAPI.md`; `Image Metadata Format.md` |
| `ListModels` (folder, depth, `subtype` such as `LoRA`), `DescribeModel`, `ListLoadedModels` are read routes under `fundamental_model_access`. `DescribeModel` returns title, author, description, trigger phrase, tags, license, architecture and standard size: text set by model authors | `ModelsAPI.md` |
| `GetCurrentStatus` returns queue counts (`waiting_gens`, `loading_models`, `waiting_backends`, `live_gens`) and backend status. `InterruptAll` with `other_sessions: false` interrupts only generations from the calling session | `BasicAPIFeatures.md` |
| `ListImages` (path, depth, sort, filter) returns saved images with `metadata`, usually a JSON string holding the full parameters including the prompt. Image files are fetched with `GET <server>/View/...` | `T2IAPI.md`; `Image Metadata Format.md` |
| Prompts have their own tag syntax in angle brackets: `<lora:...>`, `<embed:...>`, `<wildcard:...>` (reads files under `Data/Wildcards` on the server), `<preset:...>`, `<param[...]:...>` (sets any generation parameter), `<segment:...>` (can trigger a first-use model download), `<setvar>`, `<macro>`, `<repeat>`, `<region>` | `Prompt Syntax.md` |
| SwarmUI's own web interface uses this same API (watching the browser's network calls shows the exact requests) | `docs/API.md` |
| **The mcpopus host returns tool results as text only** (`render_output` converts to a string and caps it), so a plugin cannot return an image to Claude. The host has no tool-call timeout of its own | mcpopus `src/mcpopus/manager.py` |

## 3. Decisions in the plan

**Transport.** HTTP only in the first versions: `GetNewSession`, then `GenerateText2Image` (blocking) run as a background task inside
the plugin, so the tool can answer within a bounded wait and report progress by job id. This needs no WebSocket library, so no host
change. A later version may add `GenerateText2ImageWS` for live progress; it needs `wsproto` in the host image, the same dependency the
Home Assistant plugin's area features already need.

**Own HTTP client.** Like the Home Assistant plugin, the plugin builds its own `httpx2.AsyncClient` so it can honour a custom
certificate authority and a verify flag, sends the `swarm_token` cookie itself, and closes the client in `teardown`.

**Sessions.** One session id, kept in memory only, fetched on first use and again when Swarm answers `invalid_session_id` (retry once).
Nothing is persisted; a reload starts a new session.

**Least privilege comes from SwarmUI's own roles.** The plugin's token should belong to a dedicated Swarm user whose role holds only
`fundamental_generate_tab_access`, `fundamental_model_access`, `basic_image_generation` and `view_image_history`, plus a model folder
allow list. `status` reads the `permissions` list from `GetNewSession` and **warns when the token's user holds any permission the
plugin never needs** (it lists the permission names, which are not secret). Without account protection, every request runs as the
instance owner, so the docs tell the owner to turn on `AuthorizationRequired` first.

**Plugin-side limits as well**, because Swarm's own limits are not tuned for an AI that can ask a hundred times: every limit below is a
setting with a safe default and is enforced before anything is sent.

**No image content.** Claude will not see the pictures. Results say what was made, the saved file path, and a link to open it in
SwarmUI (from the `public_url` setting). This is a host limitation (section 2). Smallest host change if wanted later: let a tool return
an MCP image content block (a small `Image` result type handled in `render_output`, size-capped), and have the plugin fetch a bounded
preview from `/View/...`. A resize needs an image library the host image does not have, so the plugin would pass through Swarm's own
file or refuse files over a size limit. Not assumed, not worked round.

## 4. Tools

| Tool | Tier | Phase | Notes |
| --- | --- | --- | --- |
| `status` | read | 1 | Reachable, Swarm version, queue counts, backend status, the token user's name, permission summary and any unneeded-permission warning, the limits in force |
| `list_models` | read, untrusted | 1 | Checkpoints (`subtype` Stable-Diffusion) in a folder, paged, filtered by the plugin's model policy; name, class, standard size, loaded flag |
| `list_loras` | read, untrusted | 1 | Same for `subtype` LoRA |
| `describe_model` | read, untrusted | 1 | One model's title, trigger phrase, tags, standard size. Free text from the model author, so untrusted and capped |
| `list_parameters` | read | 1 | The generation parameters the plugin accepts (the intersection of Swarm's list and the plugin's allow list), with ranges |
| `list_images` | read, untrusted | 1 | Recent images in the history: path, date, a trimmed summary of the prompt and model. Prompts are other people's text; filtered to this user's own history |
| `generate_image` | write | 2 | Starts a generation and waits up to `wait_seconds`; returns finished image paths or a job id |
| `get_generation` | read | 2 | Status of a job started by this plugin: queued, running (percent if available), done with paths, failed |
| `cancel_generation` | write | 2 | `InterruptAll` with `other_sessions` false, so only this plugin's session |

Tier reasoning: generating creates files and spends GPU, which is a change outside the plugin, so it is `write` and off until the owner
ticks it by name. Nothing is `admin` in the planned versions.

`generate_image` arguments: `prompt`, `negative_prompt`, `model`, `width`, `height`, `steps`, `cfg_scale`, `seed` (-1 means random),
`images` (count), `loras` (a list of `{name, weight}`), and an optional `extra` object of further parameters, which must be ids returned
by `list_parameters` and listed in the owner's `allowed_parameters`.

## 5. Safety rules for `generate_image`

All enforced by the plugin, server side, before the request is sent. Violations are refused with a message that names the argument and
the limit (never echoing a hostile prompt).

1. **Prompt syntax is off by default.** A prompt containing `<` is refused unless `allow_prompt_syntax` is on. Reason: the tag syntax can
   load models and LoRAs by name, read wildcard files from the server's disk, set any parameter, and trigger downloads. Plain prompts and
   the `(word:1.2)` weighting form still work. The `loras` argument is the supported way to use a LoRA.
2. **Models and LoRAs** must exist (checked against `ListModels`), match the plugin's `model_allowlist` or not match `model_denylist`
   (globs, deny wins), and pass Swarm's own folder policy. A refused or unknown model gets the same answer.
3. **Parameter allow list.** Only the named arguments plus `allowed_parameters` (default empty) are sent. No init image, no output path,
   no ControlNet or image prompting, no backend selection, no video or audio parameters in version 1.
4. **Size limits** (settings): images per call (default 2, at most 8), pixels per image (default 1 MP, at most 4 MP), width and height
   multiples of 64 within the model's range, steps (default at most 40), `cfg_scale` within the parameter's range.
5. **Concurrency and rate**: one running job by default (`max_concurrent_jobs`, at most 4), a generations-per-hour limit, and a
   per-minute limit, on top of the host's own. A queue length check against `GetCurrentStatus`: refuse when Swarm already has more than
   `max_queue` waiting.
6. **Disk and privacy**: outputs are saved in the Swarm user's history (normal behaviour); `donotsave` is not offered. The plugin never
   deletes images in the first versions.
7. **No secret in any result.** The token and cookie are read at the moment of use; the session id, token and server URL are never
   returned or logged; tests grep results, logs and audit for them.
8. **Everything from Swarm is untrusted data**: model descriptions, image metadata and prompts in the history, backend messages.
   Returned size-capped and marked untrusted, never acted on.
9. A **content policy** (blocked words in prompts) is not built in; Swarm's own role settings, a model allow list and Claude's own
   rules are the controls. A `blocked_prompt_terms` setting can be added if Alex wants it (open question).

## 6. Configuration (planned)

| Setting | Default | Notes |
| --- | --- | --- |
| `base_url` | required | `http(s)://host:7801`, no credentials, query or path |
| `auth_token` | empty (secret) | A Swarm auth token (sent as the `swarm_token` cookie). Needed when the instance requires accounts, which it should |
| `verify_tls`, `ca_certificate` | true, empty | As in the homeassistant plugin |
| `public_url` | empty | Where a person opens SwarmUI in a browser, for links in results. Optional |
| `model_allowlist`, `model_denylist` | empty | Globs on model names (`SDXL/*`); deny wins |
| `allowed_parameters` | empty | Extra parameter ids `extra` may set |
| `allow_prompt_syntax` | false | Allows `<...>` tags in prompts |
| `max_images_per_call`, `max_pixels`, `max_steps` | 2, 1 048 576, 40 | Size limits |
| `max_concurrent_jobs`, `max_queue`, `max_per_hour`, `max_per_minute` | 1, 3, 30, 4 | Rate and queue limits |
| `wait_seconds` | 45 | How long `generate_image` waits before returning a job id (5 to 120) |
| `default_model` | empty | Used when the caller names none |
| `timeout_seconds`, `max_response_bytes` | 20, 4 000 000 | Per request (not the generation call) |
| `job_timeout_seconds` | 600 | Longest a started generation is waited for before the job is marked failed and interrupted |
| `instructions` | empty | Text for the client |

## 7. Failure messages (planned)

Every message names a setting or says what to check; none contains the URL, token, session id or text Swarm sent.

| Situation | Message (start) |
| --- | --- |
| Cannot connect, timeout, certificate, redirect, not JSON | As in the homeassistant plugin, naming `base_url`, `timeout_seconds`, `ca_certificate` |
| Account required, no or bad token | `SwarmUI needs a valid token. Check the auth_token setting ...` (paused for 60 seconds after one refusal, as in homeassistant) |
| Permission missing | `The SwarmUI user behind this token may not do that (permission <name> is missing).` The permission name is not secret |
| Session expired | handled silently (one retry); a second failure says so |
| Swarm busy | `SwarmUI already has N jobs waiting (max_queue). Try again later.` |
| Job failed or interrupted | `The generation did not finish.` plus Swarm's error text only if it passes a length and character filter |
| Limit exceeded | names the limit and its setting |
| Model not allowed or unknown | `No such model.` (one answer) |

## 8. Out of scope (deliberately)

Anything in AdminAPI or BackendAPI (backends, restarts, server settings, users); model download, delete, rename, load and metadata
editing; wildcard and preset editing; user settings and token creation; ComfyUI workflow and the Comfy backend routes; image deletion;
`OpenImageFolder` (opens a folder on the server); video, audio, upscaling pipelines, image-to-image, inpainting, ControlNet and
image prompting (all need image inputs, which the plugin cannot safely take); grid generation and batch tools; returning images to
Claude (host limitation); multi-user impersonation (`impersonateUser`).

## 9. Unverified and open questions

1. Parameter ids and ranges for the Swarm version Alex runs: the plugin must read them from `ListT2IParams` at run time, not hard-code
   them. The ids in section 2 come from the docs' examples.
2. Whether a LoRA is passed as a `loras` and `loraweights` parameter or only as a prompt tag in the Swarm version in use. If only as a
   tag, the plugin builds the `<lora:name:weight>` tag itself from validated arguments (the one place it writes tag syntax).
3. Whether `GenerateText2Image` returns promptly with an error when the queue or backend is unavailable, or waits.
4. The size of a Swarm `/View/` file and whether Swarm can serve a smaller preview (relevant only if the host later returns images).
5. How the `swarm_token` cookie behaves with and without `AllowLocalhostBypass`.
6. How `permissions` names look in the release in use (the plugin treats the list as opaque names and compares with its own list).
7. Whether to add a content blocklist (`blocked_prompt_terms`) and whether prompts should be logged by length only (planned: length only).
8. GPU and disk cost per generation on Alex's hardware; the defaults above are guesses to be tuned after the first live run.

## 10. Phases

1. **Read-only** (`status`, models, LoRAs, describe, parameters, history). Design verified facts are in this document; tests against a fake
   SwarmUI over real HTTP that records every request; live check by Alex. Stop and report.
2. **Generation** (`generate_image`, `get_generation`, `cancel_generation`) after Alex confirms phase 1 and sets the limits and model
   list. Fake Swarm records every generation request and asserts none is sent when the tool is off or a rule refuses. Stop and report.
3. **Optional**: live progress over WebSocket (needs `wsproto`), and image return if the host gains image results.

## 11. Test plan

A fake SwarmUI served over real HTTP, driven by the real host manager and a real MCP client, as for the other plugins. It implements
`GetNewSession` (with a permission list and session expiry), `GetCurrentStatus`, `ListModels`, `DescribeModel`, `ListT2IParams`,
`ListImages`, `GenerateText2Image` (with a controllable delay and failure), `InterruptAll`, and records every request, and separately
every request that could change something or use the GPU. Tests: every read tool; generation refused when the tool is off (zero
generation requests at the fake); every limit and the prompt-syntax rule refused before any request; hidden and missing models
identical; the token, session id and URL never in results, logs or audit; hostile model descriptions and image metadata returned as
data; session expiry retried once; an unneeded-permission warning; blocked and unreachable servers; bounded waits and job polling;
cancel affects only this plugin's session.

## 12. Acceptance checklist for Alex (sketch, finalised with the code)

Create a dedicated SwarmUI user with a role holding only the four permissions in section 3 and a model allow list, turn on
`AuthorizationRequired`, create an auth token for that user, then enter `base_url`, `auth_token`, `public_url` and a model allow list in
the plugin. Ask Claude: run `status` (expect version, queue, the user name and no permission warning); list models and LoRAs
(expect only the allowed ones); describe one; list recent images; try a model you did not allow (expect "No such model.").
After phase 2: generate one small image, check it appears in SwarmUI under that user; try a prompt with `<lora:x>` (refused); try
a request over the size limit (refused); cancel a long job; check that the token user cannot do anything else in the SwarmUI UI.

## 13. Decisions needed from Alex

1. Where SwarmUI runs and whether account protection is on (or can be turned on). Strongly recommended before any write tool.
2. Whether to accept that Claude cannot see the pictures (links and metadata only) or to ask for the host change in section 3.
3. The defaults for size, count and rate in section 6, and which models and LoRAs Claude may use.
4. Whether prompt tag syntax should ever be allowed.
5. Whether a content blocklist is wanted.
6. Whether image-to-image, upscaling or video should be considered later (they need image inputs and a separate design).
