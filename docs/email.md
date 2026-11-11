# email: design

Written 4 November 2026. Specification of the `email` plugin (endpoint `/email`), version 0.2.0. It reads a mailbox over any IMAP server (generic IMAP, decided
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

## 10. Proposal: new-mail notification (design only, nothing built, 11 November 2026)

Alex asked (11 November 2026) for the plugin to poll the configured folders and call back to Claude when new mail arrives. This
section sets out what is possible and what needs deciding. It would be a new release (0.3.0) behind its own acceptance gate.

### 10.1 What is and is not possible

- **Polling is feasible.** The host runs a plugin's `setup`/`teardown`; a plugin may start an asyncio task in `setup` and stop it in
  `teardown` (host 0.3.0 `manager.py`, read 11 November 2026). The loop would use the existing read-only backend (`EXAMINE`,
  `STATUS`, `UID SEARCH`, `BODY.PEEK[HEADER.FIELDS]`), so nothing is marked read.
- **A claude.ai connector cannot be pushed to.** MCP over HTTP is request and response; claude.ai does not listen for server
  notifications and an open conversation cannot be woken from outside. So "call Claude" needs a target that can start or wake
  something. That target is Alex's choice (10.4). I have not verified any vendor's endpoint from this sandbox and will not build one
  against a guessed API.
- **Rule 8 in DESIGN.md** (no network work in `setup` that can fail the load) still holds: the task starts in `setup` but connects
  only on its first tick, and a failing tick is logged and retried with backoff, never raised.

### 10.2 Proposed behaviour

| Item | Proposal |
| --- | --- |
| Setting `notify_url` | Empty by default, which means polling is off and the plugin is exactly 0.2.0. `https://` only. Treated as a secret (`SecretStr`), because webhook URLs often carry the credential. Never logged or returned |
| Setting `notify_token` | Optional `SecretStr`, sent as `Authorization: Bearer ...` |
| Setting `notify_folders` | Folders to watch, default `INBOX`. Each must pass the same readable/blocked check as every tool, so a blocked folder is never polled |
| Setting `poll_seconds` | Default 300, range 60 to 3600. A new connection per tick (decision 2 kept). IDLE is not used |
| What counts as new | A UID higher than the last one seen in that folder, same `UIDVALIDITY`. A changed `UIDVALIDITY` resets the baseline silently. The first tick after start only records the baseline, so a restart never replays the mailbox |
| Where the baseline lives | In memory only. After a restart or reload mail that arrived while the plugin was down is not reported (stated in the guide). Persisting it needs a data directory the host does not provide yet |
| Payload | `{"event": "new_mail", "count": N, "folders": {"INBOX": [uid, ...]}}`, at most 20 uids per call, one POST per tick at most, no sender, no subject, no body |
| Retries | One try per tick; on failure the uids are kept and re-sent next tick, capped at 200 pending, oldest dropped first with a logged count |
| Tools | `mailbox_status` gains a line: polling on or off, last tick time, last result (ok, failed with a cause), pending count. No new tool, so no new tier |
| Failure text | Names the setting, never the URL: "The notification URL refused the call. Check notify_url." |

### 10.3 Safety

- **Untrusted text stays out of the payload.** Sender and subject are written by strangers. A notification that starts a Claude
  session with that text in its prompt would be a direct prompt-injection route. Metadata-only means the session must call
  `list_messages` (which is `untrusted=True`) to see anything. This is why sender and subject are not in the proposed payload.
- **Outbound destination is the owner's alone.** The URL is a setting, not a tool argument; no model can set or change it. Host
  must be a public name; no IP literals, no `localhost`, no private ranges (checked after resolution too, against redirects: redirects
  are not followed). An optional `notify_ca_certificate` is not offered; a private target is a later decision.
- **No new capability for the model.** The plugin still has only read tools. The only write is the outbound POST, which the model
  cannot trigger or shape. Policy tick for it: none, because it is not a tool; the owner turns it on by filling `notify_url`.
- **Loop bounds.** One POST per tick, 10 second timeout, payload under 2 KB, 20 uids. A flood of mail cannot cause a flood of calls.
- A test asserts the fake IMAP server still never receives `SELECT`, `STORE`, `COPY`, `APPEND` or `EXPUNGE`.

### 10.4 Decisions for Alex

1. **What does "call Claude" mean?** Options: (a) a Claude Code scheduled/API-triggered routine that starts a session when its URL is
   called (needs its URL and token; I have not verified the request format); (b) a generic webhook to something you run (ntfy,
   Home Assistant, a script) that tells you or wakes Claude your way; (c) no push at all: instead a `new_mail_since` tool Claude calls
   at the start of a chat. (c) needs no outbound call and no stored state but only works when you are already talking to Claude.
2. **Payload detail.** Metadata-only as proposed, or also sender and subject (more useful, but injection-exposed)?
3. **Baseline persistence.** Accept in-memory (a restart skips mail that arrived meanwhile), or ask for a host data directory first?
4. **Folders and interval.** INBOX only and 5 minutes, or a different list?

### 10.5 Plan once decided

Tests with the real host manager and a fake IMAP server plus a fake webhook receiver over TLS: baseline on first tick, new UID
detected once, no replay after `UIDVALIDITY` change, blocked folder never polled, URL and token absent from logs and tool output,
no redirects followed, private addresses refused, retry and cap, payload contains no mail text, plugin stops on `teardown`. Each
protection gets a mutation check. Version 0.3.0, README table, `docs/PLUGINS.md`, `docs/BUILD_LOG.md`, `docs/GUIDE_EMAIL.md`.
Not verifiable here: delivery to any real Claude endpoint; that stays on the live checklist.
