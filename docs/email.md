# email: design

Written 4 November 2026. Specification of the `email` plugin (endpoint `/email`), version 0.1.0. It reads a mailbox over IMAP and is
built and tested against Gmail's documented behaviour. It never changes the mailbox. Conventions and the rules every plugin follows
are in [DESIGN.md](DESIGN.md) section 1; the host's plugin contract is in the mcpopus repository (`docs/PLUGINS.md`).

## 1. Goal

Let Claude read the mail of one dedicated mailbox (the owner's plan is a Gmail address created for Claude, not the personal one):
list recent messages, search, and read one. Nothing else in 0.1.0.

## 2. Decisions

| Decision | Reason |
| --- | --- |
| IMAP with the Python standard library (`imaplib`, `email`, `ssl`), run in worker threads | No new dependency, so no host change or image rebuild. The Gmail REST API with OAuth would need a Google Cloud project, a consent screen and token refresh storage; that is a later option behind the same backend interface |
| Login with a username and an app password | Gmail IMAP accepts an app password (the account needs 2-step verification). Open question 1 |
| Read-only by construction | Mailboxes are opened with `EXAMINE` and bodies fetched with `BODY.PEEK`, so no flag changes, not even `\Seen`. No tool has write or admin tier. The test fake records every command and fails the test if `SELECT`, `STORE`, `COPY`, `APPEND` or `EXPUNGE` arrives |
| TLS only (implicit TLS, normally port 993), certificate verified | The app password must never cross the network in clear. STARTTLS is out of scope until needed |
| A new connection per tool call | Nothing to keep alive, nothing lost on reload, no stale session. Gmail allows 15 simultaneous IMAP connections; calls are limited to two at once |
| All output is `untrusted=True` | Mail is written by other people |
| No write tools in this plugin | It reads untrusted content, so it does not carry tools that act (rule from the host guide). Sending would be a separate plugin or a later release behind a recipient allowlist |

## 3. Tools

All are tier `read`. Tools that return message content are `untrusted=True`.

| Tool | Arguments | Returns |
| --- | --- | --- |
| `mailbox_status` | none | Whether the login works, the server's greeting-level facts (host, user, number of visible mailboxes). No content. Not marked untrusted |
| `list_mailboxes` | none | Visible mailboxes (Gmail labels appear as mailboxes) with special-use attributes such as `\All`, `\Sent`. Names are written by the owner, so not marked untrusted |
| `list_messages` | `mailbox="INBOX"`, `unread_only=False`, `limit=20` | Newest first: id, date, sender, subject, size, unread marker |
| `search_messages` | `mailbox`, `from_address`, `to_address`, `subject`, `text`, `since`, `before`, `unread_only`, `limit` | Same format as `list_messages`, all given criteria combined with AND |
| `read_message` | `mailbox`, `uid`, `max_chars` | Headers, a text body (HTML converted to text, remote content never fetched), attachments listed by name, type and size only |

A message is identified by mailbox plus UID, shown as `INBOX:1234`. UIDs are stable until the server changes the mailbox's
UIDVALIDITY (Gmail rarely does); `read_message` shows UIDVALIDITY so a stale id can be noticed. Dates are `YYYY-MM-DD`.

## 4. Configuration

| Setting | Default | Notes |
| --- | --- | --- |
| `imap_host` | `imap.gmail.com` | Host name only. A scheme, a path or a port in it is rejected with a message naming the setting |
| `imap_port` | 993 | 1 to 65535 |
| `username` | required | The full address |
| `password` | required (secret) | For Gmail an app password. Spaces are removed when the host is Gmail, because Google displays app passwords in groups |
| `ca_certificate` | empty | PEM text of an extra certificate authority, for a private mail server. Leave empty for Gmail |
| `readable_mailboxes` | empty | Only these mailboxes are visible (one per line). Empty means all except blocked ones |
| `blocked_mailboxes` | `[Gmail]/Spam`, `[Gmail]/Trash` | Never visible, whatever else is set. A blocked mailbox is reported exactly as a missing one |
| `max_body_chars` | 20 000 | Ceiling for `read_message` text (1 000 to 200 000) |
| `max_fetch_bytes` | 2 000 000 | Most bytes downloaded for one message (a larger message is read from its start and marked truncated) |
| `timeout_seconds` | 20 | Per network operation |

Values are trimmed. Invalid values give an error naming the field, never the value.

## 5. Failure messages

| Situation | Message to the model |
| --- | --- |
| Host name does not resolve | `Cannot find the mail server. Check the imap_host setting.` |
| Connection refused or times out | `Cannot reach the mail server. Check imap_host and imap_port, and that the host can make outbound connections.` |
| TLS verification fails | `The mail server's certificate was not accepted. Check imap_host, or set ca_certificate for a private server.` |
| Login refused | `The mail server refused the sign-in. Check the username and password settings. Gmail needs an app password, not the account password.` |
| Mailbox missing or blocked | `No such mailbox.` |
| Malformed argument | A sentence saying which argument and the rule, never echoing hostile text |
| Anything unexpected | `The tool failed.` (host default), with the exception class logged |

## 6. Safety measures

- Mailbox names, UIDs, dates and search strings are validated before they reach the protocol: no control characters, quotes and
  backslashes are escaped, mailbox names are encoded as IMAP modified UTF-7 and quoted by the plugin (`imaplib` does not quote them).
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

1. Whether Google still allows IMAP login with an app password for a new account today, and the exact set-up steps in the account
   settings. Not verified in this build; the owner must try it (`mailbox_status` is the check).
2. Non-ASCII search text is sent as a quoted UTF-8 string after `CHARSET UTF-8`. Gmail's behaviour with that is unverified; the fake
   server accepts it. If Gmail rejects it, switch to an IMAP literal.
3. Whether Gmail's IMAP shows a message's labels cheaply (`X-GM-LABELS`) is worth adding for filtering. Not built.
4. Which mailbox, and whether `[Gmail]/All Mail` should be the default instead of `INBOX`. Left as `INBOX`.
