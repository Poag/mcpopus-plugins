# obsidian: Bases (`.base`) support

Written 9 November 2026. Extends the `obsidian` plugin ([DESIGN.md](DESIGN.md) section 4) so that Claude can read Obsidian Bases files,
later check what a base would show, and, only if the owner opts in, write them. Three phases, each ending in a stop and a report:

| Phase | Plugin version | Adds | State |
| --- | --- | --- | --- |
| 1 | 0.4.0, 0.4.1 | `list_bases`, `read_base` (read); `.base` counts in `vault_status`; base links resolve in `get_note_metadata` | built, tested against fakes, not yet run on the real vault |
| 2 | later | `query_base` (read): what a base would show, from a verified subset | not started |
| 3 | later | `write_base`, `edit_base` (write, off by default) | not started; waits for the owner to confirm phases 1 and 2 |

Existing tools keep their names and behaviour. `.base` paths are never accepted by `read_note`, `write_note` and the other note tools.

## 1. Verified facts

Everything here was read on 9 November 2026. "Source" is a file fetched from GitHub, read as text, never executed. The Obsidian help site itself
was not reachable from the build machine; the help source repository was.

### 1.1 The `.base` format and Bases behaviour

| Fact | Source |
| --- | --- |
| A base is saved as a `.base` file; it is YAML conforming to a schema; it can also be embedded as a `base` code block in a note | obsidianmd/obsidian-help (branch `master`) `en/Bases/Bases syntax.md`, `en/Bases/Create a base.md` |
| Top-level keys: `filters`, `formulas`, `properties`, `summaries`, `views` | `Bases syntax.md` |
| `filters` is a string, or an object with one of `and`, `or`, `not`, each a list of strings or further objects; a view also has `filters`, joined to the global ones with AND; no filters means every file in the vault | `Bases syntax.md` |
| A view has `type`, `name`, `filters`, `groupBy` (`property`, `direction`), `groupOrder`, `summaries`, plus (in the example) `limit` and `order` (a list of property names). Views may carry extra keys of their own | `Bases syntax.md` |
| Property namespaces: note (`note.x` or bare `x`), file (`file.name`, `file.path`, `file.folder`, `file.ext`, `file.size`, `file.ctime`, `file.mtime`, `file.tags`, `file.links`, `file.embeds`, `file.backlinks`, `file.properties`), formula (`formula.x`), and `this` | `Bases syntax.md` |
| Operators: `+ - * / %`, `== != > < >= <=`, `! && \|\|`; date arithmetic with duration strings (`now() - "1 week"`); functions follow JavaScript behaviour | `Bases syntax.md`, `en/Bases/Functions.md` |
| File functions: `file.hasTag(...)` (includes nested tags), `file.hasLink(x)`, `file.inFolder(folder)` (includes sub-folders), `file.hasProperty(name)`, `file.asLink()`; global `now()`, `today()`, `date()`, `if()`, `list()`, `link()` and more; string, number, list, date, link, object and regexp functions | `Functions.md` |
| Embedding: `![[File.base]]` uses the first view; `![[File.base#View]]` picks a view | `Create a base.md`, `en/Bases/Views.md` |
| `.base` is an accepted file format | `en/Files and folders/Accepted file formats.md` |
| A link to a file other than Markdown must include the extension, for example `[[Figure 1.png]]` | `en/Linking notes and files/Internal links.md` |
| The `obsidian://open` `file` parameter may omit the extension only for `.md`, so a base link keeps `.base` | `en/Extending Obsidian/Obsidian URI.md` |

### 1.2 How LiveSync stores a `.base` file

| Fact | Source |
| --- | --- |
| `isPlainText(filename)` is true only for `.md .txt .svg .html .csv .css .js .xml .canvas`; **`.base` is not in the list** | livesync-commonlib `src/string_and_binary/path.ts`, at commit `6b59815` (the commit DESIGN.md section 2 used) and on `main` (package version 0.4.1) on 9 Nov 2026: identical |
| Files that are not plain text are read as binary (`readBinary`), and the entry type is chosen from the content: a text blob is `plain`, anything else `newnote` | commonlib `src/serviceModules/FileAccessBase.ts` (`vaultReadAuto`, `adapterReadAuto`); `src/managers/EntryManager/EntryManagerImpls.ts` (`note.type = isTextBlob(data) ? "plain" : "newnote"`); obsidian-livesync `src/modules/coreObsidian/storageLib/utilObsidian.ts` |
| So **a `.base` file is stored as a `newnote` document** whose chunks are base64 text. Each chunk is the base64 of a slice of the file's bytes (`arrayBufferToBase64Single(buf.slice(i, splitEnd))`); readers decode the list of chunks one by one (`decodeBinary` on the array). A legacy form starting with `%` also exists; the library says current LiveSync always writes base64 | commonlib `src/string_and_binary/chunks.ts` (`splitPieces2V2`), `EntryManagerImpls.ts` (`data: chunks.map(e => e.data)`); octagonal-wheels `src/binary/index.ts`, `src/binary/base64.ts` (main) |
| `size` of such an entry is the file's size in bytes | same `put` code (`size: note.size`) |
| Current LiveSync is 1.0.47 and depends on commonlib 0.4.1 and octagonal-wheels 0.1.56; the plugin's interop job still pins octagonal-wheels 0.1.47 | obsidian-livesync `package.json` (`main`), `tools/package.json` here |

### 1.3 What this plugin does today with documents that are not notes (read from the code)

- The changes feed already selects `type` in `plain`, `newnote`, `notes`, so a `.base` entry **is delivered** to the plugin.
- `Vault._decode_batch` decodes it, then drops it because `is_note_path` needs a `.md` ending. It is **skipped, not counted, not an error**.
  Nothing in `vault_status` mentions it. (Before 0.4.0.)
- `Codec.assemble` already decodes `newnote` entries by base64-decoding each chunk and UTF-8 decoding the result, which matches how the library reads them. It raises `LiveSyncError` if the bytes are not UTF-8.

### 1.4 Links to bases today (read from the code, run against the fake)

- `parse.py` collects `[[...]]` targets and markdown links ending `.md`. `![[Index.base#View]]` therefore appears in `get_note_metadata`'s `links:` line as `Index.base#View`, as plain text.
- `get_note_metadata` has **no notion of a link resolving**: it never says found or not found. It also cannot show a backlink on a base, because bases were not indexed.
- So the plugin itself does not print "broken link" for a base. A report of false broken links must come from a check outside this repository that
  compares link targets with `list_notes`, which could never contain a base. **Not verified**: how the owner's weekly audit decides; check it on the live run.

## 2. Not verified (kept easy to change)

- The view `sort` key. The help page describes sorting in the interface but the syntax page's schema example has no sort key. Phase 1 shows view keys it
  finds without interpreting them; Phase 2 must not evaluate sort until it is verified on a real `.base` file. Same for view-specific keys of table, cards, list, kanban and map views (their help pages could not be found at the paths tried).
- Whether the owner's vault really has `.base` entries stored as `newnote` with base64 chunks, and with which chunk splitter. Live check in section 7.
- How Obsidian resolves a `[[Name.base]]` that matches two files. Phase 1 treats a link as pointing at every base whose path equals the target or ends with `/target` (case-insensitive, NFC), the rule `link_target_matches` already uses for notes.
- Whether the vault's LiveSync is 1.0.47. Its version document is read only by the write gate.
- `base` code blocks inside notes are not links and are not indexed in Phase 1.

## 3. Decisions

| Decision | Reason |
| --- | --- |
| New tools `list_bases`, `read_base`; existing tools do not accept `.base` | Existing prompts and tests are undisturbed. Open for the owner: extend instead? |
| YAML parsed with PyYAML `SafeLoader`, imported lazily; the plugin loads and `list_bases` and raw `read_base` work without it | **The host image has no YAML library** (mcpopus `pyproject.toml` and `requirements.lock`, checked 9 Nov 2026). Without it `read_base` returns the raw text and `parse: unavailable`. The host needs `PyYAML==6.0.3` pinned to give the parsed summary (section 8) |
| Parse guard before building Python objects: input size cap, node and depth caps, anchors and aliases refused, tags that construct objects refused (SafeLoader), one document only, top level must be a mapping | Billion-laughs expansion and object construction are the two YAML risks. The node tree is checked first, then built |
| A base larger than `max_base_bytes` (default 100 000) is refused, not cut | A truncated YAML text would mislead a later edit |
| Bases follow the note folder policy exactly: `blocked_folders` wins, `readable_folders` restricts; blocked equals missing (`No such base.`); absent from lists, counts, links and backlinks | Same rules as section 4.5 of DESIGN.md |
| `vault_status` reports base counts readable and not readable, as it already does for notes | The owner asked for it; counts only, no names |
| Link resolution for `.base` targets: a base that is blocked or missing is reported identically as `not found` | Resolution state must not reveal a blocked file |
| Plugin version 0.4.0 | New tools |

Confirmed by the owner on 9 November 2026: separate tools (not extending the note tools); a base over `max_base_bytes` is refused, not cut; `vault_status` keeps showing readable and not-readable base counts. Still open: the supported subset for `query_base`, whether `write_base` may overwrite by default, whether `.base` delete and move belong in the admin tier.

## 4. Tools (Phase 1)

| Tool | Tier | Arguments | Returns |
| --- | --- | --- | --- |
| `list_bases` | read, untrusted | `folder`, `name` (fragment), `limit` (1 to 200, default 50), `offset` | count, then `- <modified> [path](link) (<size> bytes)`, newest first; paged with the next offset |
| `read_base` | read, untrusted | `path` (ends in exactly `.base`) | link, size, modified, `parse:` status, a summary of filters, formulas, properties and views when parsing succeeded, the notes that embed or link it, then the raw YAML text |

Output is untrusted: file names, formulas and property names are text from the vault.

Parse statuses: `ok`; `failed` (not valid YAML or not a mapping, with a line number, never the text); `refused` (anchors or aliases, an object-constructing tag, too large a structure); `unavailable` (no YAML library in the host). In every case the raw text is returned.

`get_note_metadata` gains one line, only when the note links or embeds a `.base`: `bases: Index.base (found), Missing.base (not found)`.

`vault_status` gains `documents_by_extension` (counts for every document the reader sees, by lower-case extension, at most 20 entries) and `bases`: `in_vault`, `readable_here`, `not_readable_here`, `stored_as` (document types, to confirm section 1.2), `not_served` (a `.base` file with a path the plugin never serves, for example inside a dot-folder).

Setting added: `max_base_bytes` (1 000 to 5 000 000, default 100 000).

## 5. Policy and threat notes

- Every new tool runs over the same policy-filtered view as the existing ones. The index keeps bases in their own table; every list, count and link scan goes through `allows_path`.
- A blocked base and a missing base give the same message for `read_base`, are absent from `list_bases` and the counts in `vault_status` that are described as readable, and resolve as `not found` in links.
- `vault_status` already discloses a total and a readable count for notes (DESIGN.md 4.5). It does the same for bases, because the owner asked for it.
- Nothing from a base is acted upon. Formulas are never evaluated in Phase 1.
- The reader never writes. The fake CouchDB in the tests rejects writes and the tests assert none arrive.
- No secret appears in new log lines; counts and exception class names only.

## 6. Tests (Phase 1)

`tests/test_bases.py` runs the real host manager, a real MCP client and the fake CouchDB. A mutation test removes the policy check and shows the leakage tests fail. See the file for the list; it covers storage as `newnote` and as `plain`, several chunks, readable/blocked/non-writable folders, oversized and malformed YAML, anchors, object-constructing tags (with an observable side effect that must not happen), no YAML library, link resolution, counts, and unchanged existing behaviour.

## 7. Live checklist for the owner (Phase 1)

Do this after updating the plugin to 0.4.0, `blocked_folders = Medical`, and only the read tools in use.

1. Call `vault_status`. Expect `documents_by_extension` with `md` and `base`; `bases.in_vault` equal to the number of `.base` files in Obsidian; `bases.readable_here` smaller if some are in `Medical`; `bases.stored_as` showing `newnote` (if it says `plain`, tell me: section 1.2 is wrong for your version).
2. Call `list_bases`. Expect every base outside `Medical`, none inside it.
3. Call `read_base` on a small base you know. Expect `parse: ok` once the host has PyYAML, otherwise `parse: unavailable` and the raw text. Compare the raw text with the file in Obsidian: identical.
4. Call `read_base` on a base in `Medical`, and on a name that does not exist. Expect the same `No such base.` for both.
5. Call `get_note_metadata` on a note that embeds a base with `![[Name.base]]`. Expect a `bases:` line saying `found`. Do the same for a note whose link points at a base that does not exist: `not found`.
6. `read_base` on the base from step 5: the embedding note appears under `linked from`.
7. Confirm Claude cannot reach `.obsidian` or any dot-folder path, and that nothing changed in Obsidian (no new files, no conflict files).
8. Tell me what your weekly audit does with a `![[x.base]]` link; section 1.4 assumes it compares against `list_notes`.

## 8. What the host needs

- **PyYAML pinned** in mcpopus `pyproject.toml` and `requirements.lock` (and the smoke-test import list), so the parsed summary works. Phase 2 needs it; Phase 1 degrades without it.
- Nothing else for Phase 1.

## 9. Phases 2 and 3

Planned in the brief, not designed here yet. Phase 2 starts only after the live checks above and after the sort key and view keys are verified against a real `.base`. Phase 3 needs the write path for non-note documents read from the library (a `newnote` document with base64 chunks and a binary-typed entry), an extended interop job, and the owner's confirmation of phases 1 and 2.

## 10. Live results (owner, 10 November 2026, plugin 0.4.0, read-only tools)

Vault: 306 notes, 2 bases (one in `Medical`, blocked), 3 `.yaml` files.

| Step | Result | Note |
| --- | --- | --- |
| 2 `vault_status` | pass | `bases`: `in_vault` 2, `readable_here` 1, `not_readable_here` 1, `stored_as` `{"newnote": 2}`, `not_served` 0. **Section 1.2 is confirmed on this vault: a `.base` file is a `newnote` document.** The totals match the owner's screenshot; the count from Obsidian's own search was not made |
| 4, 5, 6, 7, 11 | pass | Folder and name filters; `Medical` folder answers "No such folder."; the blocked base, a missing base and a base in a missing folder all answer "No such base."; path-rule errors; no `bases:` line on a note without base links. The one readable base parsed as `unavailable` (no PyYAML in the host) and its raw text is a view with `type` and `name` only |
| 3 paging | **bug found, fixed in 0.4.1** | With one base, `offset 2` said "showing 0 to 2". It now says "none at this offset". Paging with a real next page was not exercised: the vault has one readable base |
| 1, 8, 9, 10 | prompt errors, not plugin faults | Step 1 forbade write tools in the list, but the owner has ticked them in the host policy for this connector. Steps 8 and 9 need a note that links the base; `Test/Projects/Overview.md` is empty. Step 10 used "PyYAML", which a note legitimately contains; the match was a note, not the base |

Still unverified after this run: link resolution (`found` / `not found`) and the "linked or embedded from" list against real data; paging across pages; the view `sort` key (the base has no sort); the oversize refusal on real data; behaviour with PyYAML installed.

Corrections to the live prompt: step 1 should check that `list_bases` and `read_base` exist and are read-only, not that no write tool exists; step 3 needs at least three readable bases; steps 8 and 9 need a note containing `![[Index.base]]` and one containing `![[Missing.base]]`; step 10 should search for a word that occurs only in a base.

### 10.1 Link checks on real data (10 November 2026, plugin 0.4.1, run from the build session with write access to `Test/` only)

Notes in `Test/Projects/`: `Overview.md` (`![[Index.base]]`) and `Bork.md` (`![[Missing.base]]`) written by the owner; `Link Forms.md` and `No Links.md` written by the build session.

| Check | Result |
| --- | --- |
| `Overview.md` metadata | `bases: Index.base (found)` |
| `Bork.md` metadata | `bases: Missing.base (not found)` |
| `Link Forms.md`: embed with path and view, wikilink with alias, markdown link, link to `Medical/Index.base`, link to a missing base, link inside a code fence | `Test/Projects/Index.base (found)`, `Index.base (found)` (the alias and markdown forms resolve to the same target and are listed once), `Medical/Index.base (not found)`, `Nothing At All.base (not found)`; the code-fence link is not in the `bases:` line. The blocked base answers exactly like the missing one |
| `No Links.md` | no `bases:` line |
| `read_base Test/Projects/Index.base` | `linked or embedded from (2): Link Forms.md, Overview.md`; `Bork.md` (links a different base) and nothing from `Medical` are absent |

Observed, existing behaviour, not changed: the older `links:` line (from `parse.py`, which does not mask code) still lists `Only In Code.base`. Only the new `bases:` line and the backlinks ignore code.

Still not exercised on real data: paging across pages and the oversize refusal (need more bases), the `sort` key, a distinctive search word, PyYAML. The build session cannot create `.base` files: the note tools refuse that extension by design and base writing is phase 3.
