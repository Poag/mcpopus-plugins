# Set up a plugin and use it from Claude (worked example: Obsidian)

This walks through the whole path once: a running MCP Octopus host, the `obsidian` plugin installed from this repository, a
connector in claude.ai, and a first question to Claude. Other plugins follow the same steps; only step 3 differs.

You need: the host running and reachable at a public https name (see the host's
[DEPLOY.md](https://github.com/Poag/mcpopus/blob/main/docs/DEPLOY.md)), and for this example an Obsidian vault synced with the
Self-hosted LiveSync plugin to a CouchDB you can reach from the host.

## 1. Sign in to the admin UI

1. Browse to the admin port (`http://<host>:8081`). Sign in with the admin password you set in the stack's environment.
2. The first sign-in goes to **Set up the authenticator**. Scan the QR code with an authenticator app and type the code to confirm.
3. Open **Sign-in** in the sidebar and do the same for the connector login. This is the login Claude will ask you for. Until it is
   set up, connector sign-in is refused.

## 2. Install the plugin

1. Open **Plugins**, then the **Plugin sources** section. The default source is this repository.
2. Press **Fetch**, then **Install** next to `obsidian`, and tick **I trust this code**. If the plugin lists Python packages, the table shows them and the box reads "I trust this code and its packages"; the host installs them as part of the install. The code runs inside the host, so read it first
   (there is a **browse the code** link).
3. The plugin appears on **Home** as a card, status `unloaded` or `error` until it is configured.

## 3. Configure it

Open the card (or **Plugins**, then `obsidian`) and fill in the settings form:

| Setting | What to enter |
| --- | --- |
| `couchdb_url` | The CouchDB address, for example `https://couch.example.com` (no path) |
| `database` | The LiveSync database name |
| `username`, `password` | A CouchDB user that can read the database (and write, if you will enable writes) |
| `passphrase` | The LiveSync end-to-end passphrase, if you set one; leave empty otherwise |
| `blocked_folders` | Folders the model must never see, for example `Medical` |
| `writable_folders` | Leave empty for read-only. Add one folder (for example `Claude/Scratch`) when you want Claude to write |

Press **Save and reload**. The card turns green. Run the `vault_status` tool later to check the connection and encryption.

Under **Policy** on the same page, tick the tools Claude may use and press **Save policy and reload**. Write tools stay off until you tick them.

## 4. Add the connector in claude.ai

1. In claude.ai open **Settings**, then **Connectors**, then **Add custom connector**.
2. Name it `Obsidian` and use the URL `https://<your host>/obsidian`. Leave any client ID and secret fields empty (the host supports dynamic registration).
3. Press **Connect**. You are sent to the host's sign-in page: enter the connector password and the current authenticator code, then **Authorise**.
4. Back in claude.ai, the connector lists its tools.

Each plugin is its own connector with its own URL, and a token for one plugin is refused by every other.

## 5. Try it

In a new chat, enable the connector and ask:

> List the top-level folders in my vault, then summarise the note called "Projects/Roadmap".

Then, if you enabled writes for a scratch folder:

> Create a note `Claude/Scratch/hello.md` that says hello, then read it back.

Check in Obsidian that the note appears on your devices.

## When something goes wrong

| Symptom | Likely cause |
| --- | --- |
| Card shows `error` | Open the card: the message names the setting at fault (a leading space in the URL, wrong database name, wrong password) |
| Claude says the vault is encrypted | Set `passphrase`, save, reload |
| "Writing is not enabled for that folder" | Add the folder to `writable_folders` and tick the write tools under Policy |
| Sign-in page rejects you | Use the connector password (not the admin one) and a fresh authenticator code |
| Claude cannot reach the connector | Check the public name, Caddy and cloudflared; the admin port must never be exposed |

More detail: [DESIGN.md](DESIGN.md) for how the plugin behaves, and the host's
[SECURITY.md](https://github.com/Poag/mcpopus/blob/main/docs/SECURITY.md) for what you are trusting.
