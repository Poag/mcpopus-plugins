# mcpopus-plugins: design

Written 4 November 2026. Plugins for [mcpopus](https://github.com/Poag/mcpopus), the MCP host. This document is the
design for the plugin collection and the specification of its first plugin, `obsidian`. The host's own docs
(`docs/PLUGINS.md`, `docs/SECURITY.md` in that repository) define the plugin API and trust model and are not repeated.

Status: `obsidian` 0.2.0 reads and, when enabled, writes. Reads ran against Alex's real database on 4 Nov 2026 (298 notes, matched
the existing connector). **Writes have only been tested against a simulated CouchDB**; follow section 4.11 before enabling them on the real vault. Email (IMAP) is built read-only (0.1.1); proxy is an outline only.

## 1. Scope and conventions

| Plugin | Endpoint | State |
| --- | --- | --- |
| `obsidian` | `/obsidian` | read and write tools built (0.4.0: Bases `.base` files added read-only, see [DESIGN-bases.md](DESIGN-bases.md)); writes untested against real LiveSync clients (section 4.9) |
| `email` (generic IMAP) | `/email` | read tools built (0.1.1), see [email.md](email.md) |
| `proxy` | `/<name>` | outline (section 6) |

Repository layout:

```
plugins/<name>/        one directory per plugin: plugin.toml, __init__.py, helper modules
docs/DESIGN.md         this file
tests/                 pytest; fake upstreams; fixtures
tools/                 fixture generator (Node, CI only; never part of a plugin)
```

Rules every plugin follows:

1. **Name** is the directory name: `a-z`, `0-9`, `-`, starts with a letter, at most 32 characters. It is the public path.
   Names are chosen for the connector label the owner sees, so `obsidian`, not `obsidian-livesync`; the storage
   technology belongs in the manifest description.
2. **Deploy** by copying `plugins/<name>` into the host's plugins volume (or checking out this repository there) and
   `mcpopus plugins reload <name>`. No image rebuild. A plugin may import only what the host image bundles
   (`requirements.lock` in the host repository). A plugin that needs another library changes the host first.
3. **Read-only first.** A plugin ships with read tools only. Write tools are a separate release, behind their own
   acceptance gate (section 4.9), and are off until the owner enables them by name in the host policy.
4. **Tiers** are the lowest true value: `read` changes nothing anywhere; `write` changes data the plugin can recreate or
   the owner can undo; `admin` deletes or cannot be undone.
5. **Untrusted output.** Anything that carries text written by someone other than the owner (mail, web pages, shared
   documents) is `untrusted=True`. Owner-written notes are treated the same way, because they may contain pasted
   web content.
6. **Secrets** are `SecretStr` config fields, read at the moment of use, never logged, never returned, never put in an
   exception message. Error messages name a cause (`refused the credentials`), never a URL with credentials, never data.
7. **Fail closed and look the same.** A resource the plugin may not touch is reported exactly as a resource that does not
   exist.
8. **Own the failure.** `setup` does no network work that can fail the load; connectivity problems surface on use, as a
   tool error that says what to fix.
9. **Bounded.** Every plugin states its limits (note size, scan count, cache size) as settings with safe defaults, and the
   host's output cap and rate limit apply on top.
10. **Tests** run the plugin under the real host manager, drive it with a real MCP client, and replace the upstream with a
    fake served over real HTTP. No test uses real credentials or real data. Parsers of foreign formats are tested against
    vectors produced by the foreign implementation itself, never only against our own writer.
11. **Versions** are per plugin (`plugin.toml`), semver. `min_host_version` is raised only when the plugin needs it.

## 2. What was verified for this design

All read on 4 November 2026. "Source" means the repository at the commit shown, read in a scratch directory (not executed).

| Fact | Source |
| --- | --- |
| LiveSync stores a note as a metadata document (`type` `plain`, `newnote`, legacy `notes`) with `path`, `children`, `ctime`, `mtime`, `size`, `deleted`, `eden`; chunks are `leaf` documents; ids `h:` (chunk), `h:+` (encrypted chunk), `f:` (obfuscated entry), `i:`, `ix:`, `ps:` (internal) | livesync-commonlib `6b59815` (4 Nov 2026), `src/common/models/{db.type,db.const,shared.const.behabiour}.ts` |
| A note's text is the concatenation of its chunks' `data` in `children` order; `eden` supplies chunks inline | same, `src/managers/EntryManager/EntryManagerImpls.ts` |
| Encrypted chunks carry `e_: true`; with obfuscation, `path` holds `"/\:"` plus an HKDF ciphertext of `{path, mtime, ctime, size, children}` and the cleartext fields are zero; older vaults hold the V2-encrypted path; `eden` is replaced by one encrypted entry (`h:++encrypted-hkdf` or `h:++encrypted`) | `src/pouchdb/encryption.ts` |
| The HKDF scheme needs a PBKDF2 salt kept in `_local/obsidian_livesync_sync_parameters` (`pbkdf2salt`, base64); the `%$` variant carries it inline | `src/replication/SyncParamsHandler.ts`, `src/common/models/sync.definition.ts`; octagonal-wheels `bba9d40` (0.1.47), `src/encryption/hkdf.ts` |
| Formats `%=`, `%$`, `%~` (V3), `%` (V2), `[` (V1), key derivations and iteration counts | octagonal-wheels `src/encryption/{hkdf,encryption,encryptionv3,obfuscatePath}.ts` |
| Chunk compression: `\x0eLZ\x1d` + base64 of raw DEFLATE (fflate), `~` for binary | commonlib `src/pouchdb/compress.ts` |
| Path to document id: lower-cased unless case-sensitive, a leading `_` becomes `/_`, `f:` plus a hash when obfuscated; NFC path identity is a database feature from commonlib 0.1.42 | commonlib `src/string_and_binary/path.ts`, `docs/unicode-path-identity.md` |
| Fixed test vectors for V2, V2-obfuscated path and V3 (used in `tests/test_crypto_vectors.py`; all pass) | octagonal-wheels `src/encryption/encryption.test.ts` |
| es617/obsidian-sync-mcp (MIT) reads CouchDB through commonlib's `DirectFileManipulator`, keeps an index fed by `_changes`, validates note paths, filters `.md` only | es617 `276c858` (31 Oct 2026), `ARCHITECTURE.md`, `src/{vault,note-path,parse}.ts` |
| Licences: es617 MIT; livesync-commonlib MIT; octagonal-wheels MIT | their `LICENSE` files |

Not verified, and where it matters:

- **Which storage modes Alex's vault uses.** He answered "yes" to "E2EE or obfuscated properties" without saying
  which. The `vault_status` tool answers it without exposing content. All of `%=`, `%$`, `%~`, `%`, `[` and `/\:` are supported.
- **Compression and encryption order on disk.** The source does not make it obvious; the reader peels whichever layer it
  finds (up to three). Tested both ways with the plugin's own writer. CI tests it against the real JavaScript writer.
- **Behaviour against a real CouchDB.** The tests use a fake that serves `GET /{db}`, `_all_docs` and `_changes` with the
  `_selector` filter. That CouchDB accepts `_selector` as a changes filter is from memory (es617 uses a selector on the same
  feed); this build did not talk to a CouchDB.
- **HKDF and V1 have no fixed vectors in the library's tests.** They are checked in the `interop` CI job, which runs
  octagonal-wheels 0.1.47 to write ciphertext and fixture vaults and requires the Python reader to decode them. That job
  passed on its first run (GitHub Actions run 1, 4 Nov 2026), covering `%=`, `%$`, V3, V2, V2 dynamic iterations, V1,
  obfuscated paths, compression in both orders and encrypted eden. The *document shapes* in those fixtures follow our reading of
  commonlib's `incomingEncrypt*` functions, so they prove the cryptography, not that a real LiveSync database is shaped exactly so.
  The sandbox the plugin was written in refused to execute the downloaded npm code; CI is the only place it has run.
- **PBKDF2 cost on the Pi 4.** 310 000 iterations take 0.06 s on the build machine; on a Pi 4 expect several times that,
  once per distinct salt (cached), in a worker thread.

## 3. Reuse decision for the Obsidian plugin

Chosen by Alex on 4 November 2026 (see the vault note *mcpopus - Plugins*): **a native Python port**, not a proxy to
es617's server and not a sidecar process. The running es617 server stays in service until this plugin replaces it and is
the reference to compare against. What was reused is knowledge, not code (NOTICE.md): tool names and output conventions,
path rules, tag and link rules, and the documented storage formats.

## 4. Plugin: `obsidian`

### 4.1 Goals

- Claude can read, search and list an Obsidian vault that Self-hosted LiveSync keeps in CouchDB, with the same names as
  today's read tools (`read_note`, `search_notes`, `list_notes`, `list_folders`, `list_tags`, `get_note_metadata`) and
  similar outputs, plus `vault_status`, so existing prompts keep working.
- **The owner decides which folders can reach a model**, enforced here and not only by habit: `blocked_folders` always wins,
  `readable_folders` restricts to a positive list. Alex's `Medical/` is the motivating case.
- Nothing is ever written to CouchDB by the read release. The fake database in the tests rejects writes and the tests assert none arrive.

Non-goals: attachments and non-markdown files; LiveSync's customisation sync and hidden files (`.obsidian`); conflict
resolution; running its own sync; a persistent index on disk.

### 4.2 Architecture

```
Claude -> mcpopus gateway (OAuth, policy, audit) -> obsidian plugin
                                                      |-- vault.py   index (path -> metadata), text cache, parse cache
                                                      |-- livesync.py  document model: metadata, chunks, eden, compression
                                                      |-- crypto.py    LiveSync decryption (HKDF, V3, V2, V1, path)
                                                      |-- couch.py     read-only CouchDB client (GET, _all_docs, _changes)
                                                      |-- paths.py     note-path rules and the folder policy
                                                      '-- parse.py     frontmatter, tags, links
```

- **Index.** On first use the plugin reads `_changes` (`filter=_selector`, `type` in `plain`, `newnote`, `notes`,
  `include_docs`, 500 at a time) from sequence 0, decodes each metadata document (decrypting `path` and the metadata
  where obfuscated) and keeps `path -> {id, rev, mtime, ctime, size, children, eden}` in memory. Later calls continue
  from the stored sequence, at most once a second, so edits made in Obsidian appear on the next call. A deleted
  change, a `deleted` flag or a path that is not a note removes the entry. The index holds no note text.
- **Text** is fetched on demand: the chunk ids of the notes needed go to `_all_docs` in batches of 200, chunks are
  decrypted and decompressed in a worker thread (PBKDF2 and AES must not block the host's event loop), assembled, and kept
  in a 32 MB least-recently-used cache keyed by `(id, rev)`. Text is never written to disk.
- **Tags, links and backlinks** are computed from text, cached by revision, and scanned over at most `max_scan_notes`
  notes per call. There is no persistent metadata index in this release (open question 3 in the vault note: on-demand
  was chosen because the vault is about 300 notes; revisit above a few thousand).
- **Start-up.** `setup` only builds the client and starts a background warm-up task; a CouchDB that is down does not stop the
  plugin loading. The first tool call reports the problem.

### 4.3 Tools

All `tier="read"`. Output is text; tools that return note content are `untrusted=True`, so the host wraps them as data.

| Tool | Arguments | Returns |
| --- | --- | --- |
| `read_note` | `path` | an Obsidian deep link, then the note text |
| `search_notes` | `terms` (any matches), `folder`, `tag`, `modified_after`, `limit` (1 to 100, default 20) | match count, then one entry per note: date, `[path](obsidian://...)`, a snippet; notes whose name matches rank first |
| `list_notes` | `folder`, `name`, `tag`, `modified_after`, `sort_by` (`name` or `modified`), `limit` (default 100, at most 500) | count, then entries |
| `list_folders` | none | `- Folder/Sub (N notes)` with direct note counts |
| `list_tags` | none | `- tag (N)`, most used first; frontmatter tags and inline `#tags`, code masked |
| `get_note_metadata` | `path` | link, size, created, modified, tags, outgoing links, backlinks, frontmatter |
| `write_note` | `path`, `content` | create a note or **replace** the whole note; returns a deep link. Tier `write` |
| `edit_note` | `path`, `content`, `operation` (`append` default, `prepend` after frontmatter, `replace`), `old_text` | change part of a note; `replace` needs `old_text` to match exactly once. Tier `write` |
| `move_note` | `from_path`, `to_path` | move or rename; both ends must be writable; refuses to overwrite. Tier `write` |
| `delete_note` | `path` | LiveSync soft delete. Tier `admin` |
| `vault_status` | none | JSON counts: how entry ids and chunks are stored (plain or obfuscated; none, hkdf, v1, v2, v3, compressed), whether the sync-parameters salt was found, notes in the vault and readable here, unreadable entries, changes sequence, folder policy. **Never any note content, path or name.** |

Dates are UTC (`2026-11-04T09:20`). Paths are vault-relative and end in `.md`.

From 0.4.0 there are also `list_bases` and `read_base` (read, untrusted) for `.base` files, `vault_status` reports file-extension counts and base counts,
and `get_note_metadata` adds a `bases:` line when a note links a `.base`. The note tools above never accept `.base` paths. See [DESIGN-bases.md](DESIGN-bases.md).

Path rules (same as es617's `note-path.ts`): no empty path, no leading `/`, no `..`, no NUL, no `:` (LiveSync's own id
prefixes), must end in `.md`, no segment starting with `.`, at most 1000 characters, and the LiveSync control files
(`redflag*.md`, `flag_rebuild.md`, `flag_fetch.md`) at the root are excluded.

### 4.4 Configuration (one form in the admin UI)

| Field | Secret | Default | Notes |
| --- | --- | --- | --- |
| `couchdb_url` | no | required | `http(s)://host[:port][/prefix]`; credentials in the URL are rejected |
| `database` | no | required | must exist; the plugin never creates anything |
| `username` | no | empty | |
| `password` | yes | none | use a CouchDB user with **read-only** access to this database (section 4.8) |
| `passphrase` | yes | none | LiveSync end-to-end encryption passphrase, if used |
| `vault_name` | no | `Vault` | for `obsidian://` links |
| `readable_folders` | no | empty (whole vault) | one per line |
| `blocked_folders` | no | empty | one per line; wins over everything |
| `writable_folders` | no | empty (nothing writable) | folders where write tools work, one per line; also needs the host policy to enable the write tools by name; blocked folders are never writable |
| `max_write_bytes` | no | 1 000 000 | largest note a write may store |
| `instructions` | no | empty | text shown to the AI client with the connector (up to 4000 characters; no secrets) |
| `max_note_bytes` | no | 2 000 000 | larger notes are not read |
| `max_scan_notes` | no | 5000 | bounds search and tag scans |

Host policy still applies on top (rate limit 60 a minute, output cap 64 KiB, deny and allow lists by tool name). Suggested
for Alex: `blocked_folders = Medical`, host output cap raised to 256 KiB if long notes are cut off.

### 4.5 Folder policy, precisely

- Matching is on whole path segments, after Unicode NFC normalisation and case folding. `Claude` matches `claude/x.md` but not
  `Claudex/x.md`. The comparison errs towards blocking: a decomposed spelling of a blocked folder is blocked.
- A note is readable if it is inside no blocked folder and (`readable_folders` is empty or it is inside one).
- Applied in the index views, not on output text, so **search, list, tags, folder tree, metadata and backlinks never see
  an unreadable note**: no names, no counts, no snippets, no tag contributions, no backlink from it, and a link in a readable
  note that points at an unreadable one is left out of the links list.
- Reading a blocked note, a missing note and a note in a folder that does not exist all return the same message
  (`No such note.`); listing a blocked folder returns `No such folder.`.
- `vault_status` reports counts of notes in the vault and readable here. Revealing that unreadable notes exist is a decision:
  it lets the owner confirm the policy works, and discloses only a number.

### 4.6 Errors

| Situation | Tool error (what the model sees) |
| --- | --- |
| CouchDB unreachable or slow | `The vault database is not available: cannot reach CouchDB (ConnectError).` |
| Credentials refused | `... CouchDB refused the credentials.` |
| Wrong database name | `... database not found (check the database name).` |
| Encrypted vault, no passphrase | `the vault is encrypted but no passphrase is configured` |
| Wrong passphrase or salt | notes do not appear; `vault_status` shows `unreadable_entries` rising |
| Chunk missing (sync in progress) | `That note could not be read from the database (missing chunk or wrong passphrase).` |
| Bad path or date, blocked or missing note | the messages in 4.3 and 4.5 |
| Unexpected exception | the host's generic `The tool failed.`; detail in the log without data |

### 4.7 Compatibility with LiveSync settings

Reading needs none of LiveSync's id-derivation settings: the plugin lists documents through the changes feed and takes paths
from the (decrypted) metadata, so the ID key, the case-sensitivity setting and the path-normalisation version, which matter
only when computing an id from a path, do not apply. Entries whose path cannot be decrypted are counted as unreadable.
Conflicted revisions are ignored (CouchDB's winning revision is read). `deleted` entries are hidden.

### 4.8 Security

- **CouchDB account.** Create a CouchDB user that is only a *reader* of the LiveSync database and give the plugin that. With
  it, even a bug here cannot alter the vault. The plugin still sends no write request; the tests assert this.
- **Transport.** Use `https` unless CouchDB is on the same host or a trusted network; the plugin does not downgrade or follow redirects.
- **Egress.** `couchdb_url` is set only by the admin. It is the only host the plugin contacts.
- **Memory.** Decrypted text lives in the cache while the plugin is loaded, in the host's process, like every plugin
  (trust model in the host's SECURITY.md). Unload clears it. The passphrase stays in memory for decryption.
- **Prompt injection.** Note content is returned inside the host's untrusted-data wrapper. The plugin has no write tools to be tricked into using.
- **Logs and audit.** Secrets are redacted by the host; the audit log records tool and argument names only; the plugin logs
  counts and exception class names.
- **Denial of service.** Size and scan limits; one refresh at a time; PBKDF2 and AES run off the event loop.

### 4.9 Writing

Read from livesync-commonlib `6b59815` (`DirectFileManipulatorV2.put`, `putDBEntryInternal`, `createChunks`, `deleteDBEntryByPath`,
`XXHash64HashManager`, `path2id_base`, `docs/remote-feature-compatibility.md`) and from es617's `vault.ts` and `tools.ts`:

- **Note document:** `{_id, path, ctime, mtime, size, type: "plain", children: [chunk ids], eden: {}}`, `_rev` set when replacing. `ctime` is kept
  when the note exists; `mtime` is now; `size` is the UTF-8 byte length (es617 does the same).
- **Chunk documents:** `{_id: "h:<id>", type: "leaf", data}`. The id is `base36(xxh64(piece + "-" + piece.length))`, with the length in
  UTF-16 code units (the default `xxhash64` algorithm without encryption). The implementation reproduces the library's fixed vector
  (`helloWorld` gives `37me21jj44pcc`) and the canonical xxh64 value; CI checks eight more against the real `xxhash-wasm`. Pieces are cut at line
  ends, at most 1000 characters (readers follow `children`, so the splitter is the writer's choice). A chunk that already exists is not rewritten.
- **Delete** is LiveSync's default soft delete: the same document with `deleted: true` and a new `mtime`. **Move** writes the new path (keeping `ctime`)
  and then deletes the old one.
- **Ids** follow the vault's own scheme, learned from existing notes (lower-case or case-sensitive, NFC or not, a leading `_` stored as `/_`).
  An existing note keeps its own id. If the scheme cannot be told (no mixed-case note names to learn from), writing is refused, not guessed.

Switches, all of which must agree before a byte is written:

1. **Host policy:** the write tools are not even listed until the owner turns read-only off and names them (`write_note`, `edit_note`,
   `move_note`; `delete_note` is tier `admin`).
2. **`writable_folders`** lists the folder; empty means nothing is writable. A blocked folder is never writable, and a note that cannot be read cannot be written.
3. **The database gate** (checked at most once a minute): the `obsydian_livesync_version` document exists, its generation is at most 13, and its
   `used_features` hold nothing but `encrypted-internal-metadata-v1` and `nfc-path-identity-v1`; the vault has no obfuscated paths; the id scheme
   can be told; and the chunks that the plugin can see (the first two of each of the first eight notes) are either **all unencrypted** or **all
   encrypted in the HKDF `%=` or `%$` format**. Anything else (obfuscated paths, the older V1/V2/V3 encryption, a mix, or an encrypted vault with
   no passphrase or no PBKDF2 salt) is refused with the reason, and reading still works.

**Encrypted vaults** (end-to-end encryption with plain paths, the form `vault_status` reports as `chunk_encryption: hkdf`): each chunk is written
as `{_id: "h:+<id>", type: "leaf", data: "%=<b64>", e_: true}`, encrypted exactly as LiveSync's HKDF writer does (random IV and HKDF salt, key from
the passphrase and the database's PBKDF2 salt). Note documents stay as above (paths are plain, there is nothing to encrypt in an empty `eden`). The
chunk id is `h:+` and a **keyed** hash (HMAC-SHA-256 of the text, 64 bits, key derived from the passphrase), not LiveSync's own id algorithm: readers
follow `children` and use the `h:+` prefix to know a chunk is encrypted, so the plugin shares no chunks with Obsidian's own writes, but ids reveal
nothing about the text. Before sending, the plugin decrypts its own chunks and compares them with the text. CI has the real LiveSync JavaScript
(octagonal-wheels) decrypt the encrypted chunks that the plugin wrote (`tools/verify_written.mjs`).

Safety: a write is refused if the note changed since it was read (the revision is compared, then CouchDB's own `409` is the final check);
one write at a time; the previous revision stays in CouchDB until compaction; nothing is ever force-written. Chunks are stored before the note
document, as LiveSync does, so a refused write can leave an unreferenced chunk behind that nothing points at. A CouchDB user without write
access gets a clear error and nothing is stored. Not handled: conflicted revisions (CouchDB's winning revision is the one edited), attachments.

### 4.9a How the es617 server's responsibilities are covered

| es617 | Here |
| --- | --- |
| Tools: `read_note`, `write_note`, `edit_note`, `delete_note`, `move_note`, `list_notes`, `list_folders`, `list_tags`, `get_note_metadata`, `search_notes` | all present; `move_note` arguments are `from_path` and `to_path` (a Python parameter cannot be called `from`) |
| `obsidian://` deep links in results | yes |
| `READ_ONLY` | host policy (read-only is the default; write tools are hidden until enabled by name) |
| `WRITE_FOLDERS` (unset means everything writable) | `writable_folders` (empty means **nothing** writable; stricter) |
| `MCP_INSTRUCTIONS`, `MCP_INSTRUCTIONS_FILE` | `instructions` in the admin form, passed to the client. No file variant: the admin login is the guard on who can write it |
| Readable-folder control | `readable_folders` and `blocked_folders` (es617 has none) |
| OAuth provider, bearer token, sessions, rate limiting | the host |
| HTTP transport, port, host-header guard | the host (path routing, Host allow-list) |
| Persistent encrypted search index, live `_changes` watcher | not copied: the index is rebuilt from the changes feed on start (about a second for 298 notes) and caught up on each call, at most once a second |
| CouchDB pre-flight check | `vault_status` |
| E2EE and obfuscated vaults | read: yes. write: no (refused with a reason) |
| `COUCHDB_ID_DERIVATION_KEY`, case-sensitivity setting | not needed to read; for writing the scheme is learned from existing ids |
| Filesystem mode (`VAULT_PATH`) | not supported; this host serves the LiveSync database |
| Overwriting on move | refused here |
| Concurrent-edit protection | new here (es617 writes last-writer-wins) |

### 4.10 Tests

`pytest` (66 pass and 9 interop tests skip locally; the interop job runs them in CI). `tests/livesync_writer.py` writes vaults in each storage mode; `tests/fake_couch.py` serves them over
HTTP and rejects writes; `running_plugin` loads the plugin with the real host manager and calls it with a real MCP client.

| Area | What is asserted |
| --- | --- |
| Crypto | the V2, V2 obfuscated-path and V3 vectors from the library's own tests; both iteration schemes; damaged or truncated values fail cleanly; wrong passphrase never yields text |
| Reader | plain, HKDF with obfuscated paths (mixed `%=`, `%$`, compress-then-encrypt and encrypt-then-compress, encrypted eden, inline chunk), and V1/V2/V3 vaults decode to the original notes; deleted, internal, binary and control documents are skipped; missing chunk is an error, never partial text |
| Policy | blocked and readable folders hide notes from every tool; blocked, missing and elsewhere give the same answer; no secret text, folder name or backlink leaks; segment, case and NFC matching |
| Index | edits and deletions in CouchDB appear on the next call |
| Safety | traversal and hidden paths refused; no PUT, DELETE or document POST ever reaches CouchDB; wrong credentials, wrong database, wrong passphrase give clear errors with no secret in output or logs; audit log has no content |
| Interop (CI only; passed) | fixtures written by octagonal-wheels 0.1.47 decode in every format, and the plugin serves them |

### 4.11 Acceptance on the real vault (Alex)

Reading (done 4 Nov 2026, see the vault note). **Writing, before it is relied on:** use a scratch folder and a copy of the database if you can.

1. Set `writable_folders = Scratch`; in the host policy turn read-only off and enable `write_note`, `edit_note`, `move_note` and `delete_note`.
2. Ask Claude to create `Scratch/Test.md`. Within a few seconds it must appear in Obsidian on each device, with the right text.
3. Edit it in Obsidian; ask Claude to read it (the edit must be visible), then to append a line; Obsidian must show the appended line.
4. Move it to `Scratch/Test2.md`, then delete it; both must propagate to every device. Check no conflict notes appear.
5. Confirm the old es617 server (if still running) reads the note; that `Medical/` cannot be written; and that a note edited in Obsidian during an agent edit is refused, not overwritten.
6. Only then widen `writable_folders`.

Reads (the original steps):


1. Create a read-only CouchDB user; set the plugin's config in the admin UI (`blocked_folders` = `Medical`).
2. Call `vault_status`. Expect `reachable: true`, the entry id and encryption mode, notes counts near 298 (298 notes were indexed by the current server on 4 Nov), `unreadable_entries: 0`.
3. Read three known notes; compare with the existing server. Search a word that appears in a `Medical/` note: expect no hit.
4. Edit a note in Obsidian; call `read_note` within a few seconds: expect the new text.
5. Report anything unreadable (`vault_status` counts only) before relying on it.

### 4.12 Open questions

1. Real storage mode of Alex's vault (E2EE, obfuscation, both): answered by step 2 above.
2. CouchDB address and a read-only user for the plugin; can the host reach it (same Pi, or over the tunnel)?
3. Writes: decide which folders to make writable once section 4.11 passes.
4. Is a persistent index needed (vault size growth, Pi 4 speed)? Measure after step 2.
5. Do any notes exceed 2 MB or use attachments that should be listed?

## 5. Plugin: `email` (generic IMAP)

Specified in [email.md](email.md): read-only mailbox access over any IMAP server with a password or app password, standard library only, no write tools.
SMTP drafts and sending are a later release behind a recipient allowlist, and no push (MCP is request and response).

## 6. Plugin: `proxy` — outline

Fronts an existing MCP server URL with the host's OAuth, policy and audit. The SDK client (`Client(streamable_http_client(url, http_client=...))`)
can connect to a remote server; **re-exporting its tools through a plugin is not checked** (mcpopus BUILD_LOG). Open points: tool
tiers must be assigned by name by the owner since the upstream does not declare them (default everything to `write` until
classified), upstream OAuth versus static header, schema pass-through, and a `list_changed` story. The Obsidian plugin makes this
less urgent: the first connector no longer needs it.
