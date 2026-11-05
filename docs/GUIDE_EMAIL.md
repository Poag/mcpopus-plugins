# Set up the email plugin (any IMAP server) and use it from Claude

The `email` plugin reads one mailbox over IMAP. It works with any provider that offers IMAP with a password or app password (Gmail,
AgentMail, Fastmail and similar). Use a mailbox made for Claude, not your personal one, because anyone who can email that address can
put text in front of Claude. General steps for installing a plugin and adding the connector are in [GUIDE_CLAUDE.md](GUIDE_CLAUDE.md);
this page covers what differs.

## 1. Get the IMAP details from the provider

You need the IMAP host name, the port (almost always 993, TLS), the login name (usually the full address) and a password. Providers
that require an app password (Gmail does, after you turn on 2-step verification and create one) will refuse the normal password.
Plain-text IMAP (port 143) and OAuth-only providers are not supported.

| Provider | Host | Notes |
| --- | --- | --- |
| Gmail | `imap.gmail.com` | App password; IMAP must be enabled in Gmail settings. Labels appear as folders; archived mail is in `[Gmail]/All Mail` |
| AgentMail | `imap.agentmail.to` | Login is the inbox address. See the troubleshooting table if no folders appear |
| Others | the provider's IMAP host | Check its help pages for the host and whether an app password is needed |

These rows are starting points to check against the provider's own help, not guarantees.

## 2. Install and configure

1. In the host's admin UI open **Plugins**, **Fetch**, then **Install** `email` and tick the trust box.
2. Open the plugin page and fill in the settings:

| Setting | What to enter |
| --- | --- |
| `imap_host` | The host name only, for example `imap.gmail.com` |
| `imap_port` | `993` unless the provider says otherwise |
| `username` | The login name |
| `password` | The password or app password |
| `readable_folders` | Leave empty for everything except blocked, or list the folders Claude may see (one per line) |
| `blocked_folders` | Defaults hide `Spam`, `Junk`, `Trash`, `Deleted Items`, `[Gmail]/Spam` and `[Gmail]/Trash` (and anything below them). Replacing the list removes the defaults |
| `ca_certificate`, `max_body_chars`, `max_fetch_bytes`, `timeout_seconds` | Normally leave alone |

3. **Save and reload**. Under **Policy** all five tools are read tools and are ticked by default.
4. Add `https://<your host>/email` as a custom connector in claude.ai (steps 4 and 5 of [GUIDE_CLAUDE.md](GUIDE_CLAUDE.md)).

## 3. Try it

> Run mailbox_status, then list the 5 newest unread messages in my INBOX and read the first one.

Send the mailbox a test message first if it is new. Then check with the provider (webmail or app) that the message is still unread: the
plugin must not change it.

## Tools

| Tool | Use |
| --- | --- |
| `mailbox_status` | Checks sign-in and reports counts of folders found, visible and hidden. No content |
| `list_folders` | Visible folders, with special-use marks such as `\Sent` |
| `list_messages` | Newest messages in a folder, optionally unread only |
| `search_messages` | From, to, subject, text, date range, unread. Criteria combine with AND |
| `read_message` | One message by `INBOX:1234` (folder and uid): headers, text, attachment list |

Attachments are listed but never downloaded.

## When something goes wrong

| Message or symptom | Likely cause |
| --- | --- |
| Card shows `error`, message names `imap_host` | A scheme (`https://`), port or path was pasted into the host. Use the host name only |
| "refused the sign-in" | Wrong `username` or `password`. Some providers need an app password; check that IMAP access is enabled for the account |
| "Cannot find the mail server" | `imap_host` is misspelt, or the host has no DNS |
| "Cannot reach the mail server" | Wrong `imap_port`, or outbound connections on that port are blocked from the host |
| "certificate was not accepted" | `imap_host` is not the server's name, or a private server needs `ca_certificate` |
| `mailbox_status` says 0 visible | Read the counts it gives: "hidden by settings" means check `readable_folders` and `blocked_folders`; "not understood" means the provider's folder list has an unusual form, so report it; all zero means the server lists nothing and INBOX will not open, so check that host and port are the provider's IMAP service and that the account has a mailbox |
| "No such folder" | The folder does not exist, is blocked, or is outside `readable_folders`. These look the same by design |
| Search with accented text finds nothing | See open question 2 in [email.md](email.md) |
| Messages missing from `INBOX` | They may be archived or filed elsewhere: use `list_folders`, then search the right folder |
