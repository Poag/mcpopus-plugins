# Set up the email plugin (Gmail) and use it from Claude

The `email` plugin reads one mailbox over IMAP. The recommended set-up is a Gmail address made for Claude, not your personal one,
because anyone who can email that address can put text in front of Claude. General steps for installing a plugin and adding the
connector are in [GUIDE_CLAUDE.md](GUIDE_CLAUDE.md); this page covers what differs.

## 1. Prepare the Gmail account

1. Create or choose the account. Turn on 2-step verification for it.
2. In the Google account's security settings, create an **app password** (look for "App passwords"; Google moves this page, so
   search the account settings for it). Copy the 16 characters. Spaces do not matter.
3. Gmail's own IMAP setting is normally on for new accounts. If sign-in is refused, check **Settings, See all settings, Forwarding
   and POP/IMAP** in Gmail and enable IMAP.

Whether Google still offers app passwords for your account type is not verified here: see open question 1 in [email.md](email.md).

## 2. Install and configure

1. In the host's admin UI open **Plugins**, **Fetch**, then **Install** `email` and tick the trust box.
2. Open the plugin page and fill in the settings:

| Setting | What to enter |
| --- | --- |
| `imap_host` | `imap.gmail.com` (the default). Host name only |
| `imap_port` | `993` (the default) |
| `username` | The full Gmail address |
| `password` | The app password |
| `readable_mailboxes` | Leave empty for everything except blocked, or list the labels Claude may see (one per line) |
| `blocked_mailboxes` | Defaults to `[Gmail]/Spam` and `[Gmail]/Trash`. Add any label that must stay hidden. Replacing the list removes the defaults |
| `ca_certificate`, `max_body_chars`, `max_fetch_bytes`, `timeout_seconds` | Normally leave alone |

3. **Save and reload**. Under **Policy** all five tools are read tools and are ticked by default.
4. Add `https://<your host>/email` as a custom connector in claude.ai (steps 4 and 5 of [GUIDE_CLAUDE.md](GUIDE_CLAUDE.md)).

## 3. Try it

> Run mailbox_status, then list the 5 newest unread messages in my INBOX and read the first one.

Send the account a test message first if it is new. Then check in Gmail that the message is still unread: the plugin must not change it.

## Tools

| Tool | Use |
| --- | --- |
| `mailbox_status` | Checks sign-in. Counts only |
| `list_mailboxes` | Visible labels and special mailboxes (`\All`, `\Sent`) |
| `list_messages` | Newest messages in a mailbox, optionally unread only |
| `search_messages` | From, to, subject, text, date range, unread. Criteria combine with AND |
| `read_message` | One message by `INBOX:1234` (mailbox and uid): headers, text, attachment list |

Attachments are listed but never downloaded. Gmail's All Mail is `[Gmail]/All Mail`; it is the place to search everything not in
spam or trash.

## When something goes wrong

| Message or symptom | Likely cause |
| --- | --- |
| Card shows `error`, message names `imap_host` | A scheme (`https://`), port or path was pasted into the host. Use `imap.gmail.com` only |
| "refused the sign-in" | Wrong username or password setting. Use an app password, not the account password; check 2-step verification and that IMAP is enabled in Gmail |
| "Cannot find the mail server" | `imap_host` is misspelt, or the host has no DNS |
| "Cannot reach the mail server" | Wrong `imap_port`, or outbound connections on port 993 are blocked from the host |
| "certificate was not accepted" | `imap_host` is not the server's name, or a private server needs `ca_certificate` |
| "No such mailbox" | The label does not exist, is blocked, or is outside `readable_mailboxes`. These look the same by design |
| Search with accented text finds nothing | See open question 2 in [email.md](email.md) |
| Messages are missing from `INBOX` | They are archived: search `[Gmail]/All Mail` |
