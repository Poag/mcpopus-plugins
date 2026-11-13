# email: design

Written 4 November 2026. Specification of the `email` plugin (endpoint `/email`), version 0.3.0. It reads a mailbox over any IMAP server (generic IMAP, decided
4 November 2026; Gmail and AgentMail are examples) and never changes the mailbox. Tested only against a fake server so far. Conventions and the rules every plugin follows
are in [DESIGN.md](DESIGN.md) section 1; the host's plugin contract is in the mcpopus repository (`docs/PLUGINS.md`).

## 1. Goal

Let Claude read the mail of one dedicated mailbox (the owner's mailbox is an AgentMail address created for Claude, not a personal one):
list recent messages, search, and read one. Nothing else in 0.1.0.

## 2. Decisions

| Decision | Reason |
| --- | --- |
| IMAP with the Python standard library (`imaplib`, `email`, `ssl`), run in worker threads | No new dependency, so no host change or image rebuild. The Gmail REST API with OAuth would need a Google Cloud project, a consent screen and token refresh storage; that is a later option behind the same backend interface |
| Login with a username and password (IMAP `LOGIN`) | Works with any provider that offers password or app-password IMAP. Providers that allow only OAuth (Microsoft 365, and Gmail without an app password) are out of scope. Open question 1 |
| Read-only by construction | Folders are opened with `EXAMINE` and bodies fetched with `BODY.PEEK`, so no flag changes, not even `\Seen`. No tool has write or admin tier. The test fake records every command and fails the test if `SELECT`, `STORE`, `COPY`, `APPEND` or `EXPUNGE` arrives |
| TLS only (implicit TLS, normally port 993), certificate verified | The app password must never cross the network in clear. STARTTLS is out of scope until needed |
| A new connection per tool call | Nothing to keep alive, nothing lost on reload, no stale session. Providers cap simultaneous connections (Gmail allows 15); calls are limited to two at once |
| All output is `untrusted=True` | Mail is written by other people |
| No write tools in this plugin | It reads untrusted content, so it does not carry tools that act (rule from the host guide). Sending would be a separate plugin or a later release behind a recipient allowlist |

## 3. Tools

All are tier `read`. Tools that return message content are `untrusted=True`.

| Tool | Arguments | Returns |
| --- | --- | --- |
| `mailbox_status` | none | Whether the login works, the server's greeting-level facts (host, user, number of visible folders). No content. Not marked untrusted |
| `list_folders` | none | Visible folders (Gmail labels appear as folders) with special-use attributes such as `\All`, `\Sent`. Names are written by the owner, so not marked untrusted |
| `list_messages` | `folder="INBOX"`, `unread_only=False`, `limit=20` | Newest first: id, date, sender, subject, size, unread marker |
| `search_messages` | `folder`, `from_address`, `to_address`, `subject`, `text`, `since`, `before`, `unread_only`, `limit` | Same format as `list_messages`, all given criteria combined with AND |
| `read_message` | `folder`, `uid`, `max_chars` | Headers, a text body (HTML converted to text, remote content never fetched), attachments listed by name, type and size only |

A message is identified by folder plus UID, shown as `INBOX:1234`. UIDs are stable until the server changes the folder's
UIDVALIDITY (Gmail rarely does); `read_message` shows UIDVALIDITY so a stale id can be noticed. Dates are `YYYY-MM-DD`.

## 4. Configuration

| Setting | Default | Notes |
| --- | --- | --- |
| `imap_host` | required | Host name only, for example `imap.gmail.com` or `imap.agentmail.to`. A scheme, a path or a port in it is rejected with a message naming the setting |
| `imap_port` | 993 | 1 to 65535 |
| `username` | required | The full address |
| `password` | required (secret) | The IMAP password; an app password where the provider requires one. Spaces are removed only when the host is Gmail, because Google displays app passwords in groups |
| `ca_certificate` | empty | PEM text of an extra certificate authority, for a private mail server. Leave empty for public providers |
| `readable_folders` | empty | Only these folders are visible (one per line). Empty means all except blocked ones |
| `blocked_folders` | `Spam`, `Junk`, `Trash`, `Deleted Items`, `[Gmail]/Spam`, `[Gmail]/Trash` | Never visible, nor anything below them, whatever else is set. A blocked folder is reported exactly as a missing one. Replacing the list removes the defaults |
| `max_body_chars` | 20 000 | Ceiling for `read_message` text (1 000 to 200 000) |
| `max_fetch_bytes` | 2 000 000 | Most bytes downloaded for one message (a larger message is read from its start and marked truncated) |
| `timeout_seconds` | 20 | Per network operation |

Values are trimmed. Invalid values give an error naming the field, never the value.

## 4a. Folder discovery (0.1.1)

Terminology (0.2.0): IMAP calls them mailboxes; this plugin and its settings say folders, as the Obsidian plugin does. The account is the mailbox. Settings `readable_mailboxes`/`blocked_mailboxes` and tool `list_mailboxes` from 0.1.x became `readable_folders`, `blocked_folders` and `list_folders`; re-enter any customised list.

Servers differ in how they answer `LIST`. The plugin asks for `*` and, if that gives nothing, `%`; it accepts quoted or unquoted names and `NIL`
or quoted delimiters; and if `INBOX` is missing from the list it tries to open it, because every IMAP server has one. `mailbox_status`
reports counts only: entries the server listed, usable, visible, hidden by settings, and not understood, with a hint when nothing is
visible. Added after the first run against AgentMail reported no folders (cause not yet known: open question 5).

## 5. Failure messages

| Situation | Message to the model |
| --- | --- |
| Host name does not resolve | `Cannot find the mail server. Check the imap_host setting.` |
| Connection refused or times out | `Cannot reach the mail server. Check imap_host and imap_port, and that the host can make outbound connections.` |
| TLS verification fails | `The mail server's certificate was not accepted. Check imap_host, or set ca_certificate for a private server.` |
| Login refused | `The mail server refused the sign-in. Check the username and password settings. Some providers (Gmail among them) need an app password rather than the account password.` |
| Folder missing or blocked | `No such folder.` |
| Malformed argument | A sentence saying which argument and the rule, never echoing hostile text |
| Anything unexpected | `The tool failed.` (host default), with the exception class logged |

## 6. Safety measures

- Folder names, UIDs, dates and search strings are validated before they reach the protocol: no control characters, quotes and
  backslashes are escaped, folder names are encoded as IMAP modified UTF-7 and quoted by the plugin (`imaplib` does not quote them).
- Counts and sizes are capped: result limit 50, search strings 200 characters, header values 500 characters, one message download
  `max_fetch_bytes`, text `max_body_chars`.
- HTML is converted with the standard parser; scripts, styles and comments are dropped; no link is followed or fetched. Link targets
  are kept in the text as `text (url)` so the model can see where a link points; this is the main prompt-injection and phishing
  surface and stays flagged by the untrusted wrapper.
- Attachments are never downloaded or returned.
- The password is read when connecting and never logged or returned; a test searches all results and log output for it.

## 7. Out of scope for 0.1.0

Sending or drafting mail, marking read, labels, moving or deleting, attachments, Gmail raw search (`X-GM-RAW`), threads, STARTTLS,
OAuth, push notifications, and more than one mailbox account per plugin.

## 8. Open questions

1. Whether a given provider still allows IMAP password login (Gmail: app password; unverified). Gmail itself has not been tried. The owner's first real run used AgentMail (`imap.agentmail.to`), where sign-in succeeded.
2. Non-ASCII search text is sent as a quoted UTF-8 string after `CHARSET UTF-8`. Verified on 5 November 2026 against AgentMail (a search for "Zürich" found the message); other servers, Gmail included, are unverified; the fake
   server accepts it. If Gmail rejects it, switch to an IMAP literal.
3. Whether Gmail's IMAP shows a message's labels cheaply (`X-GM-LABELS`) is worth adding for filtering. Not built.
4. Which folder is the default. Left as `INBOX`.
5. Why AgentMail's IMAP service reported no folders on 4 November 2026 with 0.1.0. Possibly settings, an unusual `LIST` reply, or a service that exposes mail differently. 0.1.1 reports the counts; the owner's next `mailbox_status` result decides.

## 9. Verified against a real server

5 November 2026, plugin 0.1.1 on AgentMail (`imap.agentmail.to`), through a claude.ai connector, by the 13-step test prompt: sign-in, folder listing
(6 entries, 4 visible after the default blocks), unread listing, search by subject, text, accented text and date range, reading plain and
attachment messages, an injection message returned as quoted data and not followed, messages still unread afterwards, missing folder and
bad uid errors. The only failed step was a test-prompt expectation of `[Gmail]/All Mail`, which does not exist on a non-Gmail account.
Not yet verified: Gmail itself, 0.2.0 on a real server, a message over `max_fetch_bytes`, STARTTLS (not built).

## 10. New-mail notification (0.3.0, decided 11 November 2026)

Alex asked (11 November 2026) for the plugin to poll for new mail and call back to Claude. Decisions: (1) the callback is an API call to
Claude's own endpoint, (2) metadata only, (3) the baseline lives in memory only, (4) watch every visible folder; a downstream
"handoff agent" decides where each message goes.

### 10.1 What is and is not possible

- **Polling is feasible.** The host runs a plugin's `setup`/`teardown`; a plugin may start an asyncio task in `setup` and stop it in
  `teardown` (host 0.3.0 `manager.py`, read 11 November 2026). The loop uses the read-only backend (`EXAMINE`, `UID SEARCH`), so
  nothing is marked read.
- **A claude.ai connector cannot be pushed to**, so the callback goes to the endpoint that starts a Claude Code session: the **Fire URL
  of a scheduled task (formerly routine) with an API trigger**. Read on 11 November 2026 from `code.claude.com/docs/en/routines`:
  `POST https://api.anthropic.com/v1/claude_code/routines/trig_<id>/fire`, headers `Authorization: Bearer <token>`,
  `anthropic-beta: experimental-cc-routine-2026-04-01`, `anthropic-version: 2023-06-01`, JSON body `{"text": "..."}`; the text is freeform,
  not parsed, and arrives wrapped in a `<routine-fire-payload>` block labelled untrusted. A success returns `routine_fire` with a session URL.
  **The endpoint is research preview**: shapes, limits and token semantics may change (the plugin will show the failure, not hide it).
  The token is scoped to that one task and is created on the web only (claude.ai/code/routines, Edit, Trigger, API, Generate token).
  The task's saved prompt must reference the payload explicitly, or the text is treated as inert context (10.6).
- **Limits (docs): 30 API fires an hour per task, 100 an hour per account.** The plugin keeps well under: `poll_seconds` is at least 120 and
  at most 20 fires are made in any rolling hour.
- **Rule 8 in DESIGN.md** holds: the task starts in `setup` but connects only on its tick; a failing tick is logged and retried, never raised.

### 10.2 Behaviour

| Item | Behaviour |
| --- | --- |
| `notify_url` | Empty by default, which means polling is off and the plugin is exactly 0.2.0. Otherwise it must match the Fire URL form above exactly (`https`, host `api.anthropic.com`, path `/v1/claude_code/routines/trig_<letters and digits>/fire`, no port, user, query or fragment). Anything else is refused at load with a message naming the setting. The destination is therefore fixed to Anthropic by the validator, not by trust in the owner's typing |
| `notify_token` | `SecretStr`, required when `notify_url` is set. Read when sending, never logged or returned |
| `notify_folders` | Empty (default) means every visible folder, so `readable_folders` and `blocked_folders` apply; a blocked folder is never opened. A list narrows it. Gmail note: `[Gmail]/All Mail` and `[Gmail]/Sent Mail` are visible by default and would report the same message again or your own sent mail; list the folders you want to avoid that |
| `poll_seconds` | Default 300, 120 to 3600. A new connection per tick |
| New mail | A UID above the last one seen in that folder under the same `UIDVALIDITY`. The first look at a folder (including after a restart or a changed `UIDVALIDITY`) only records a baseline, so nothing is replayed. Baseline is in memory only: mail that arrives while the plugin is down or reloading is not reported (decision 3) |
| Payload `text` | One line of JSON: `{"event":"new_mail","count":N,"more_pending":M,"folders":{"INBOX":[uid,...]}}`. Folder names and numeric ids only: no sender, subject, date or body (decision 2) |
| Per tick | One POST at most, 20 uids at most. The rest wait (200 pending at most, oldest dropped first and counted in the log) |
| Failure | A failed POST keeps the ids for the next tick. A 401 or 403 pauses sending for an hour (names `notify_token`); 404 names `notify_url`; 429 waits for the next tick. Errors name the setting, never the URL or token |
| `mailbox_status` | Gains one line: off, or on with folders watched, interval, last check, last result, pending count |
| Tools | None added, so no new tier. The only write is the outbound POST; no tool argument can set, change or trigger it |

### 10.3 Safety

- **Untrusted text stays out of the payload.** Sender and subject are written by strangers. A notification that starts a session with
  that text would be a direct injection route; with ids only, the session must call `list_messages` (which is `untrusted=True`).
- **Fixed destination, no redirects, no caller-chosen URL.** Redirect responses are not followed. The HTTP client verifies certificates
  with the system store and has no verification switch.
- **Bounded.** Request timeout 10 seconds, payload under 2 KB, 20 uids, 20 fires an hour, at most 50 folders per tick.
- **Read-only IMAP is kept**: a test asserts the fake server never receives `SELECT`, `STORE`, `COPY`, `APPEND` or `EXPUNGE`.
- **Blast radius of a leaked token** is the one task's trigger, and fire text is wrapped as untrusted by Anthropic (docs). A leaked
  token can start sessions of that task, up to its rate limit; rotate it on the task's page.

### 10.4 Owner set-up (summary; the step-by-step is in GUIDE_EMAIL.md)

Create a scheduled task whose saved prompt tells Claude what to do with a new-mail notice, add an **API trigger**, copy the Fire URL and
generate the token, then put the URL and token in the plugin's settings. The task needs the email connector attached if it should read
the mail. The prompt must say to read the `routine-fire-payload` block, fetch the messages by id with `read_message`, and treat them
as untrusted (10.6).

### 10.5 Tests

Real host manager and MCP client, fake IMAP over TLS, and a fake Fire endpoint over TLS. The plugin's HTTP client is given a transport
that redirects the connection to the fake while the request still names `api.anthropic.com`, so the tests prove what the plugin
actually sent (URL, headers, body). Covered: off by default; URL and token validation; baseline then one report, no replay; changed
`UIDVALIDITY`; blocked folders never opened; `notify_folders` narrowing; ids only (no subject or sender); retry after a 500; pause after a
401; redirects not followed; 20-id and hourly caps; secrets absent from logs, results and audit; read-only commands only; task stops on
unload. Each protection gets a mutation check.

### 10.6 Not verified

Delivery to the real endpoint, the token, and what the task does with the text: the sandbox has no route to a real trigger and no token
should be placed here. The live check is in 10.7.

### 10.7 Live check (for Alex)

Built as 0.3.0 on 11 November 2026 and tested only against fakes. To check it for real: create the scheduled task and API trigger as in
GUIDE_EMAIL.md section 4, enter the Fire URL and token, **Save and reload**, then:

1. Run `mailbox_status` and confirm it says notifications are on and, after the first check (up to `poll_seconds`), "nothing new waiting".
2. Send the mailbox one test message from another address. Within `poll_seconds` a session of the scheduled task should start; open the
   session URL from the task's run list and confirm the notice names the folder and the message id and carries no subject or sender.
3. Check the message is still unread in the mail client.
4. Restart the plugin (Save and reload), send another message, and confirm exactly one session starts (no replay of earlier mail).
5. Optional: rotate the token on the task page without updating the plugin, send a message, and confirm `mailbox_status` says to check `notify_token`.

## 11. Proposal: sending, drafts, attachments and mailbox changes (design only, nothing built, 13 November 2026)

Alex asked (13 November 2026) to add the remaining email features: sending, attachments and the rest. This section sets out what they
involve and what must be decided before any code. Each group below would be its own release behind its own acceptance gate
(DESIGN.md rule 3), starting with the safest.

### 11.1 The risk that shapes everything

A session that reads mail from strangers, holds private data and can send mail has all three parts of the usual exfiltration route:
text written by an attacker, access to the owner's data, and a way out. A hostile message could try to make the session mail the
owner's data to the attacker. Decision 7 of section 2 already says reading plugins carry no acting tools. The controls below follow
from that; none of them removes the risk, they limit what a successful injection can do.

### 11.2 Feature groups, in the order I would build them

| Group | Tools (tier) | Notes |
| --- | --- | --- |
| A. Attachments, read side (0.4.0) | `list_attachments`, `read_attachment` (read, untrusted) | Tool results are text only. Returns text for text-like types (plain text, CSV, calendar, HTML converted as in 6) with a size cap; for PDF only if a pinned text-extraction package is added through `requirements` (host 0.3.0 installs it; the package, its hashes and the memory cost need checking first); images and other binaries are described (name, type, size), never returned. Nothing is written to disk. Attachment parsing of hostile files is a real attack surface and gets its own limits (decompression, page and nesting caps) |
| B. Mailbox changes (0.5.0) | `mark_read`, `mark_unread` (write); `move_message` (write); `delete_message` (admin) | The first use of `SELECT`, `STORE`, `COPY` and `EXPUNGE`, so the read-only test fakes and the "never writes" assertions change: reading tools still must never issue them. Delete would move to the Trash folder, not expunge, and only the owner's named folder is a valid destination (no folder creation). Each tool off until ticked by name in the host policy |
| C. Drafts (0.6.0) | `create_draft` (write) | `APPEND` to the Drafts folder only. The owner reviews it in the mail client. Nothing leaves the mailbox |
| D. Sending (0.7.0) | `send_draft` (write) and, only if chosen in 11.5, `send_message` / `reply_to_message` (write) | SMTP over TLS. See 11.3 |
| E. Outgoing attachments | within D | Only plain text attachments created by Claude from its own text (for example a `.txt`, `.csv` or `.ics`), or a forward of an attachment from a message already in the mailbox. There is no file access in the host, so no file can come from the owner's disk or vault |

### 11.3 Sending controls (group D)

| Control | Proposal |
| --- | --- |
| Recipient allowlist | Setting `allowed_recipients`: exact addresses or whole domains. **Empty by default, so nothing can be sent.** A recipient not on it is refused with a message naming the setting. Replying to the sender of a message in the mailbox is a separate switch, `allow_reply_to_sender` (default off), because the From address is written by the sender |
| Fixed sender | The From address is the configured mailbox; no tool argument can change it, nor add Reply-To, Bcc or arbitrary headers |
| Recipients per message | At most 3 (setting). Cc is only allowed to allowlisted addresses |
| Rate | At most 5 an hour and 20 a day (settings), counted in memory and persisted nowhere (a reload resets it; stated in the guide) |
| Content | Plain text only; subject 200 characters, body 20 000 (settings); no remote content; control characters and header injection rejected before SMTP; the plugin builds the message, never a raw string from the model |
| Review | Recommended in claude.ai: set the send tool to "Needs approval". In draft-first mode (11.5) the owner also reads the full text in the Drafts folder before approving |
| Autonomous sessions | The scheduled triage and water tasks should **not** have the sending connector attached. Sending belongs in an interactive chat with approval. The guide will say so |
| Audit | The host's audit log records each call; the plugin logs recipient count and result, never addresses, subject or body |

### 11.4 Settings added (all optional; none set means 0.3.1 behaviour)

`smtp_host`, `smtp_port` (465, implicit TLS; STARTTLS on 587 only if chosen in 11.5), `smtp_username` and `smtp_password` (default: the IMAP
login), `from_name`, `allowed_recipients`, `allow_reply_to_sender`, `max_recipients`, `max_per_hour`, `max_per_day`, `max_subject_chars`,
`max_body_chars_out`, `drafts_folder`, `trash_folder`, `max_attachment_bytes`. Values are trimmed and errors name the setting, never the value.

### 11.5 Decisions for Alex

1. **Same plugin or a separate one?** My recommendation: a separate plugin (working name `email-send`) with its own endpoint, so a
   connector that only reads can never gain a send tool by a settings change, and so the sending connector is attached only where needed.
   Cost: its own login settings and its own tests. Alternative: tools in `email`, off until ticked in the host policy.
2. **Draft-first or direct send?** Draft-first (`create_draft` then `send_draft` by id) lets you read the exact text in your mail client
   before approving; direct send is quicker but the approval prompt shows only what the tool call carries. Recommendation: draft-first.
3. **Recipient policy:** allowlist only, or allowlist plus replying to the sender of a received message?
4. **Mailbox changes:** which of mark read or unread, move and delete do you want, and into which named folders may mail be moved?
5. **Attachments:** what do you need to read (PDFs? images are not possible)? For sending: is "text files made by Claude, or forwarding a
   received attachment" enough?
6. **SMTP details:** the AgentMail SMTP host and port, and whether it sends through the same login. I could not check: the AgentMail
   documentation host (`docs.agentmail.to`) is outside this session's allowed network domains. Gmail and others differ.
7. **Autonomous sessions:** confirm that no scheduled task gets the sending connector.

### 11.6 Tests and verification plan

Real host manager and MCP client, a fake IMAP server that now also accepts `APPEND`, `STORE`, `COPY` and `EXPUNGE` (recorded, and
asserted never to arrive from a read tool), and a fake SMTP server over TLS. Cases: recipient not on the allowlist, header injection in
every argument, rate limits, size caps, From not changeable, the reply switch, attachment limits and hostile attachment files, secrets
absent from logs, results and audit, tools absent until enabled. A mutation check for each protection. Not verifiable here: a real
provider's SMTP behaviour, its sending limits and spam handling, deliverability, and the real approval prompt; those go on a live checklist
with real addresses filled in.
