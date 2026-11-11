# obsidian: Bases (`.base`) support

Written 9 November 2026. Extends the `obsidian` plugin ([DESIGN.md](DESIGN.md) section 4) so that Claude can read Obsidian Bases files,
later check what a base would show, and, only if the owner opts in, write them. Three phases, each ending in a stop and a report:

| Phase | Plugin version | Adds | State |
| --- | --- | --- | --- |
| 1 | 0.4.0, 0.4.1 | `list_bases`, `read_base` (read); `.base` counts in `vault_status`; base links resolve in `get_note_metadata` | built, tested against fakes, not yet run on the real vault |
| 2 | 0.5.1 | `query_base` (read): what a base would show, from a verified subset | built; first live run 10 Nov 2026 (`file.name` corrected in 0.5.1); checklist items 2 to 4 still open (section 11) |
| 3 | 0.6.0 | `write_base`, `edit_base` (write, off by default) | built and tested against fakes and the real LiveSync JavaScript code (CI interop job); **not yet run on the real vault** (section 12.8) |

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

- The view `sort` key. **Seen once on 10 Nov 2026 in two files written by the owner's Obsidian (section 10.2), so no longer unknown, but one sample only.** The help page describes sorting in the interface but the syntax page's schema example has no sort key. Phase 1 shows view keys it
  finds without interpreting them; Phase 2 must not evaluate sort until it is verified on a real `.base` file. Same for view-specific keys of table, cards, list, kanban and map views (their help pages could not be found at the paths tried).
- Whether the owner's vault really has `.base` entries stored as `newnote` with base64 chunks, and with which chunk splitter. Live check in section 7.
- How Obsidian resolves a `[[Name.base]]` that matches two files. Phase 1 treats a link as pointing at every base whose path equals the target or ends with `/target` (case-insensitive, NFC), the rule `link_target_matches` already uses for notes.
- Whether the vault's LiveSync is 1.0.47. Its version document is read only by the write gate.
- `base` code blocks inside notes are not links and are not indexed in Phase 1.

## 3. Decisions

| Decision | Reason |
| --- | --- |
| New tools `list_bases`, `read_base`; existing tools do not accept `.base` | Existing prompts and tests are undisturbed. Open for the owner: extend instead? |
| YAML parsed with PyYAML `SafeLoader`, imported lazily; the plugin loads and `list_bases` and raw `read_base` work without it | **The host image has no YAML library** (mcpopus `pyproject.toml` and `requirements.lock`, checked 9 Nov 2026). Since host 0.3.0 the plugin lists `PyYAML==6.0.3` in its own `requirements` (0.4.2) and the host installs it; if it is missing `read_base` returns the raw text and `parse: unavailable` (section 8) |
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

- **Nothing to change in the host** from 10 Nov 2026: host 0.3.0 installs the plugin's `requirements` (PyYAML, hashed for the Python 3.13 image). The owner applies the plugin update on the Plugins page. Phase 2 needs it; Phase 1 degrades without it.
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

### 10.2 Second live run (10 November 2026): paging, search and the `sort` key

Plugin on the owner's host was still **0.4.0** (the past-the-end paging header bug was still present), so that line reflects 0.4.0.

| Check | Result |
| --- | --- |
| `vault_status` | `bases`: in_vault 4, readable_here 3, not_readable_here 1, `stored_as` `{"newnote": 4}`, `not_served` 0. `documents_by_extension`: md 309, base 4, yaml 3. `unreadable_entries` 0 |
| Paging, 3 readable bases | `limit 2`: "showing 1 to 2. Use offset 2 for the next page"; `limit 2, offset 2`: "showing 3 to 3", the third base, no repeat. `offset 50`: "showing 0 to 50" (the 0.4.0 bug; 0.4.1 says "none at this offset") |
| Search word only in a base (`zzqx`, in `Search.base`'s filter) | `search_notes`: 0 notes; `list_notes name ".base"`: 0 notes |
| `Medical/Index.base` and a missing base | both "No such base." |

Real `.base` text written by Obsidian (two files, version of Obsidian not recorded): a view has `type: table`, `name`, optional `filters` (`and:` list of strings such as `file.name == "zzqx"`), `order` (a list of property names, including `file.*` ones and the vault's own frontmatter names) and `sort`, a list of mappings with `property` and `direction` (`ASC`). The top-level `filters` and `formulas` keys were not present in these samples.

Not exercised: the oversize refusal (the larger base is 802 bytes and the setting cannot go below 1 000; covered by a test only), parsing with PyYAML, and view types other than `table`.

## 11. Phase 2: `query_base` (0.5.0)

Goal: Claude cannot see Obsidian's rendering, so `query_base` works out which notes a base's chosen view would list, and with which columns, from the plugin's own
index. It is read tier, untrusted output, and runs over the same policy-filtered view as every other tool. The rule from the brief: only constructs that are documented
or seen in a real file are evaluated; **anything else is refused by name and no rows are returned, never a partial or guessed result.** The owner confirmed the subset
proposed on 10 Nov 2026 ("Start phase 2").

### 11.1 Tool

`query_base(path, view="", limit=50, offset=0)`: `path` ends in `.base`; `view` is a view name (default: the first view, as Obsidian does for an embed); `limit`
1 to 100 and `offset` page through the matches. Needs PyYAML to read the base (otherwise refused, saying why).

### 11.2 What is evaluated (the subset)

| Area | Supported |
| --- | --- |
| Views | `type: table` only. View keys: `type`, `name`, `filters`, `order`, `sort`, `limit` (all seen in the syntax page or in real files, sections 1.1 and 10.2). Top level: `filters`, `formulas`, `properties`, `summaries`, `views`; `formulas` and `summaries` may be defined but not used |
| Filters | a string, or `and` / `or` / `not` groups of strings and groups; global and view filters joined with AND (documented). `not` means none of the entries is true (documented: "None of the following are true") |
| File properties | `file.name` (without extension for a note, with it for any other file; both verified live), `file.basename`, `file.path`, `file.folder`, `file.ext`, `file.size`, `file.ctime`, `file.mtime`; `file.tags` as a column |
| Note properties | frontmatter, as `x`, `note.x` or `note["x"]`; values are typed (text, number, true/false, list, date) |
| File functions | `file.hasTag("a", "b")` (any of; nested tags included), `file.inFolder("f")` (sub-folders included), `file.hasLink("x")`, `file.hasProperty("p")` |
| Global functions | `now()`, `today()`, `date("YYYY-MM-DD")` and `date("YYYY-MM-DD HH:mm:ss")` |
| Operators | `== != < > <= >=`, `&& \|\| !`, `+ - * / %`, parentheses; `null`, `true`, `false`, numbers, quoted text |
| Dates | a date plus or minus a duration text such as `"1 week"` or `"36h"` (units: seconds, minutes, hours, days, weeks), and the fields `.year .month .day .hour .minute .second`, and `.date()` |
| Text and list methods | `contains`, `containsAll`, `containsAny`, `startsWith`, `endsWith`, `isEmpty`, `lower`, `trim`, and `.length` |
| Columns | the view's `order` list: file properties above and note properties |
| Sort | the view's `sort` list of `property` and `direction` (`ASC`, `DESC`), first entry most important (documented for the interface; the YAML shape is from real files) |
| `limit` | the view's own limit is applied after sorting and reported |

### 11.3 Refused, by name, with no rows

Formulas (`formula.x`, as a filter, column or sort), `this`, any function or method not listed (`if`, `link`, `list`, `number`, `min`, `max`, `format`, `relative`, `replace`,
`split` and so on), regular expressions, `file.backlinks`, `file.links`, `file.embeds`, `file.properties`, `file.fullname` (seen in a real file, meaning not documented),
`file.tags` inside a filter (use `file.hasTag`), year and month durations (calendar arithmetic), subtracting two dates, `groupBy`, `groupOrder`, view `summaries`, view types other than
`table`, any other view key, any other top-level key, empty filter groups, expressions over 1000 characters or 300 nodes, and anything the parser cannot read. Each refusal says
what was found and that nothing was computed.

### 11.4 Meaning chosen where the documentation is silent (all **unverified against Obsidian**)

| Question | Choice here | Visible in the result |
| --- | --- | --- |
| Which files are rows | Obsidian lists every file; this index has Markdown notes with contents, and the names, sizes and times of other files. **Only notes are listed.** The same filter is also evaluated on the other files (using file properties only) and their number is reported | "N other files also match and are not listed" |
| `file.name` | **Corrected in 0.5.1 and 0.6.1:** a note's name excludes the extension (owner, 10 Nov 2026: `file.name == "zzqx"` listed `zzqx.md`); any other file keeps it (owner, 11 Nov 2026: `file.name == "Index.base"` gave one row in Obsidian; 0.5.1 to 0.6.0 used the basename for every file, so a filter on a non-note's name counted wrongly among the "other files") | |
| Missing value in `<`, `>`, `<=`, `>=` | false, as JavaScript | not flagged |
| Equality | text, numbers (not true/false), dates compare by value; `null == null`; a missing value equals only `null`; **different types never equal** (JavaScript's loose `"5" == 5` is not applied) | a comparison across types is not matched and the number of notes this happened to is reported |
| Relational with mixed types, arithmetic on mismatched types, a number or text used as a true/false | not matched, counted as above | "N notes had a comparison between values of different types and were treated as not matching" |
| Text comparison and sort | exact and case-sensitive for `==`, `contains`; sort uses case-folded text | |
| Dates | all arithmetic in **UTC**; `today()` is midnight UTC; a date property without a time is midnight UTC; the host clock is used and shown | the header states the clock time and "UTC" |
| Sort when the base has none | path order | "no sort in the base: path order" |
| Sort with missing values | missing values last, both directions | |
| `hasLink` | matches a link in the note by name or path suffix, like the plugin's backlinks | |
| `hasTag` | case-insensitive, `#` ignored, nested tags match | |
| Notes whose contents cannot be read | evaluated as having no tags, links or properties **and reported** | "N notes could not be read; the result may be incomplete" |
| Frontmatter that is not valid YAML | treated as no properties, reported | "N notes have frontmatter that could not be read" |
| `inFolder` | case-insensitive | |

### 11.5 Policy and safety

- Only readable notes are evaluated. A blocked note is not a row, is not counted (not even among the "other files"), contributes nothing to tags, links or sort order, and a `hasLink` is evaluated on the text of readable notes only. A blocked base answers `No such base.`
- The expression language has its own small parser and evaluator. Nothing is run with `eval`; the length, depth, node and step budgets are enforced and a query that exceeds them is refused.
- Frontmatter is read with the safe YAML loader under the same guards as bases (no aliases, no object tags, size and depth caps).
- Property values come from notes, so everything is returned inside the untrusted wrapper, each cell clipped. Error messages quote only identifiers (letters, digits, `_`, `.`), clipped, never base text.
- Notes' contents are fetched only when the query needs tags, links or properties; a query needing contents on more than `max_scan_notes` readable notes is refused rather than cut.
- Output is capped (100 rows a page, cells clipped to 100 characters).

### 11.6 Result header

Every result states: it was computed by mcpopus and may differ from Obsidian; the clock and UTC; the filters as understood; the sort; the supported constructs (one line); counts (matches,
shown, other files, warnings).

### 11.7 Live checklist (owner), after updating to 0.5.0

1. `query_base` on `Test/Projects/Search.base` (filter `file.name == "zzqx"`): expect 0 notes and the header. Then make a note named exactly `zzqx.md` under `Test`: **if it does not match, `file.name` excludes the extension in your Obsidian**, and 11.4's first row is wrong; tell me.
2. A base with `file.inFolder("Test")`: compare the listed notes with the same base opened in Obsidian. Differences in order or in attachments are expected and shown in the header.
3. A base filtering on a frontmatter property and on a tag: compare the counts.
4. A base with `file.mtime > now() - "7 days"`: compare with Obsidian; note the time zone difference near midnight.
5. A base filtering on `file.inFolder("Medical")`, or a note in `Medical` that matches a filter: expect it nowhere, not even in a count.
6. A base using a formula or `groupBy`: expect a refusal naming it.
7. Check nothing changed in the vault.

### 11.8 Open decisions for the owner

- Whether to accept strict refusal of unknown view keys (it will refuse bases carrying display keys we have not seen, for example column widths; send a sample and the key is added once verified).
- Whether to list non-note rows (attachments, bases) by name, which widens what Claude can see; for now they are only counted.
- Whether `groupBy` should be applied in a later version (it changes the order and arrangement of rows, so it is refused now).

## 12. Phase 3: `write_base` and `edit_base` (0.6.0), off by default

Written before the code. The owner's go-ahead was "lets proceed with phase 3" (11 Nov 2026). Checklist items 2 to 4 of 11.7 (`inFolder`, property and tag filters, `mtime`) have not been run on the real vault, so phase 2 is not fully live-verified; this is recorded, not hidden.

### 12.1 Verified facts used (sources read on 11 Nov 2026)

| Fact | Source |
| --- | --- |
| A file that is not plain text is saved as `{_id, path, ctime, mtime, size, type: "newnote", children, eden: {}}`; `type` is `plain` for a text blob and `newnote` otherwise; `size` is the byte size; `_rev` is set when replacing | livesync-commonlib `main`, `src/managers/EntryManager/EntryManagerImpls.ts` (`putDBEntryInternal`: `note.type = isTextBlob(data) ? "plain" : "newnote"`, `size: data.size`) |
| Each chunk of a binary entry is `{_id, type: "leaf", data}` where `data` is the base64 of a slice of the file's bytes (`arrayBufferToBase64Single(buf.slice(i, splitEnd))`, each slice encoded on its own, with padding). Readers decode the `children` one by one, so the slice size is the writer's choice. Small files are one chunk | same repository, `src/string_and_binary/chunks.ts` (`splitPieces2V2`); live: the owner's four bases are `newnote` (10 Nov 2026) |
| The chunk id is a hash of the chunk's `data` string as stored (the base64 text), exactly as for text | `createChunks`/`prepareChunk` (hash of the piece) |
| Views: layouts `table`, `list`, `cards`, `kanban`, `map`; kanban needs `groupBy`; map needs the Maps plugin | obsidian-help `en/Bases/Views.md` (master, 11 Nov 2026) |
| Top-level keys `filters`, `formulas`, `properties` (name to `displayName`), `summaries`; view keys `type`, `name`, `limit`, `groupBy` (`property`, `direction`), `groupOrder`, `filters`, `order`, `summaries`; views may carry other keys of their own | `en/Bases/Bases syntax.md` (master, 11 Nov 2026); `sort` seen in the owner's real files (section 10.2) |
| A `.base` is not LiveSync-internal and not `.md`; this plugin never writes anything but `.base` here | section 1.2 |

Not verified: that the owner's Obsidian and phone accept a `.base` written by this path (the live checklist, 12.8); LiveSync's behaviour when Obsidian and this plugin write the same base at once (conflicts are not merged, as for notes).

### 12.2 Tools (tier `write`, off until the owner ticks them by name in the host policy)

- `write_base(path, content, overwrite=False)`: creates a base. If a base exists at the path it is **refused** unless `overwrite` is true.
- `edit_base(path, old_text, content)`: exact string replace. `old_text` must occur exactly once (not found, or more than once, is refused). No append or prepend: a base is YAML and a blind append is rarely valid.

Both return: the path, the open link, `created` or `replaced` or `edited`, the size, and **`sha256 before`** (`none` for a new base) and **`sha256 after`**, hashes of the UTF-8 bytes. No content is returned or logged.

### 12.3 What a path and a write must satisfy (all must hold; first failure is the message)

1. The path ends in exactly `.base` (case-sensitive), is vault-relative, has no `..`, `:` or backslash segments tricks, and **no segment starts with a dot**, so `.obsidian` and every hidden folder are never reachable (the existing `validate_base_path`).
2. The folder is in `writable_folders` and not in `blocked_folders`; both tools use the note folder switches (empty `writable_folders` means nothing is writable). A refusal says "Writing is not enabled for that folder." and does not say whether the path exists.
3. The database gate of 4.9 passes (LiveSync generation, features, no obfuscated paths, storage mode plain or HKDF `%=`/`%$`, id scheme known).
4. Size: the text is at most `max_base_bytes` (the same limit as reading, so a base written here can always be read back) and at most `max_write_bytes`.
5. Text: valid UTF-8, no NUL or other control characters except tab and newline, no leading BOM. `\r\n` is converted to `\n` and a final newline is added; the response says so when it happened.
6. The YAML parses with `parse_base` (PyYAML `SafeLoader`, one document, no anchors or aliases, no object tags, node and depth caps). If PyYAML is missing the write is refused saying why, never skipped.
7. **The structure matches what is verified** (12.4).
8. Overwrite and edit are checked against the revision that was read; a change made elsewhere in the meantime is refused (`409` is the final check), as for notes.
9. At most 10 base writes a minute from this plugin, in addition to the host's per-tool limit.

### 12.4 The verified structure (checked on the text a write would store)

Refused, naming the first problem and never quoting the text, unless:

- top level is a mapping whose keys are only `filters`, `formulas`, `properties`, `summaries`, `views`;
- `views` is a non-empty list (at most 30) of mappings; each has `type` one of `table`, `list`, `cards`, `kanban` (a kanban view must have `groupBy`; from 0.6.1, because Obsidian itself wrote a kanban base with `type`, `name`, `groupBy` and `order` on 11 Nov 2026, section 12.11; `map` needs the Maps plugin and its keys are not verified, so it is refused) and only the keys `type`, `name`, `filters`, `order`, `sort`, `limit`, `groupBy`, `groupOrder`, `summaries`;
- `name` is text up to 100 characters; `limit` is a whole number from 1 to 10 000 (not true or false); `order` is a list of at most 200 texts of at most 200 characters; `sort` is a list of mappings with exactly `property` (text) and `direction` (`ASC` or `DESC`); `groupBy` is a mapping with `property` (text) and `direction` (`ASC` or `DESC`); `groupOrder` is a list of texts;
- `filters` (global or view) is a text, or a mapping with exactly one of `and`, `or`, `not`, a non-empty list (at most 100) of texts or further such mappings, nested at most 8 deep; each text is at most 1000 characters;
- `formulas` and `summaries` map a name to a text up to 1000 characters; `properties` maps a name to a mapping with only `displayName` (text);
- every name and text is free of control characters.

Not checked, and said in the tool description: the **expressions** inside filter, formula and summary texts (Obsidian's expression language is only partly verified, section 11), so a base can be accepted here and still show an error in Obsidian. `query_base` can then be used to see whether the plugin can evaluate it. **This also means `edit_base` refuses a base that already carries view keys outside the list** (for example column widths written by Obsidian); the result of an edit is checked, not only the change. That is the same strict rule as phase 2 and an open decision (12.9).

### 12.5 The documents written

Same sequence as notes: chunks first (only those not already present), then the entry document with `_rev` when replacing; one write at a time; the previous revision stays until compaction.

- Entry: `{_id, path, ctime, mtime, size: <bytes>, type: "newnote", children: [...], eden: {}}`. `ctime` is kept for an existing base, `mtime` is now. The id follows the vault's learned scheme (4.9) and an existing base keeps its own id. **Extra check for bases:** if the vault already holds bases, their ids must agree with the scheme at the same 99 per cent threshold as notes, otherwise the write is refused.
- Chunks: the UTF-8 bytes are cut at newlines into slices of at most 24 000 bytes (a 100 kB base is five chunks at most; the cut can never fall inside a base64 group because each slice is encoded alone), each slice base64-encoded with padding. An empty text is refused (a base has at least a `views` list).
  - Plain vault: `{_id: "h:" + xxh64 id of the base64 text, type: "leaf", data: <base64>}`, the id exactly as the library computes it for any chunk.
  - HKDF vault (`vault_status` reports `chunk_encryption: hkdf`, plain paths and ids): `{_id: "h:+<keyed id>", type: "leaf", data: "%=" + HKDF ciphertext of the base64 text, e_: true}`, as for notes; the entry document is not encrypted (plain paths, empty `eden`).
- Before sending, the plugin decodes its own documents through the same reader that reads the vault (`Codec.assemble`, decrypting if needed) and compares the bytes with what it meant to store; a mismatch refuses the write.
- Not handled, as for notes: conflicted revisions, internal LiveSync files.

### 12.6 Delete and move: left out of this version

`delete_note` and `move_note` stay note-only (`.md`); a `.base` path is refused by them. Reasons: the owner has not decided whether they belong in the admin tier (section 3), a deleted base silently breaks every `![[x.base]]` embed, and the tool set can be extended without changing the write path. Consequence for testing: a base created through Claude is deleted in Obsidian by the owner, and the "no conflict file after move and delete" item is deferred until the owner decides. Asked in 12.9.

### 12.7 Tests (real host manager, real MCP client, fake CouchDB; every protection gets a mutation check)

Tools listed only when ticked; `writable_folders` empty, blocked folder, other folder, `.obsidian` and any dot-folder, wrong extensions (`x.md`, `x.Base`, `x.base.md`, `x.base/`, `.base`), traversal, size limit, control characters, BOM, CRLF, invalid YAML, aliases, object tags, unknown top-level and view keys, unsupported view types, bad `limit`/`sort`/`filters` shapes; create then read back with `read_base` and `query_base`; overwrite refused by default and allowed with the flag; edit not found, ambiguous, valid, and one that makes the structure invalid; a concurrent change refused; hashes in the response equal the stored bytes; the stored documents have `type: newnote`, base64 children, `size` in bytes, a kept `ctime`; plain and HKDF modes; the gate; rate limit; nothing logged; counters; no write touches a note or any other document. CI: `tools/gen_fixtures.mjs` and `tools/verify_written.mjs` are extended so that the real octagonal-wheels code decrypts the chunks the Python writer produced for a base **and** the library's own binary decoder (`decodeBinary`) reconstructs the exact bytes.

### 12.8 Live checklist (owner), to run after updating to 0.6.0, with `writable_folders` limited to `Scratch`

1. Tick `write_base` and `edit_base` under Policy; set `writable_folders` to `Scratch` only; reconnect the connector.
2. Ask Claude to create `Scratch/Probe.base` (a table view with the filter `file.inFolder("Test")` and columns `file.name`, `file.path`). Open it in Obsidian on **desktop** and on the **phone**: it must open as a base and show the notes.
3. Edit it in Obsidian (add a column), wait for sync, ask Claude for `read_base`: the change must be there. Then ask Claude for `edit_base`, and check Obsidian shows it.
4. Ask Claude to `write_base` the same path again without `overwrite`: refused. With `overwrite`: replaced, and no conflict file appears.
5. A write into `Medical` or any blocked folder, and into `Test/.obsidian/x.base`: refused.
6. Check nothing was written to `.obsidian` and no other file changed.
7. Delete `Scratch/Probe.base` in Obsidian; `list_bases` must stop listing it.

### 12.9 Decisions for the owner

- **Overwrite default:** off (a write to an existing base is refused unless `overwrite` is true). Decided here as the safe default; tell me to change it.
- **Delete and move of a `.base`:** not provided in 0.6.0 (12.6). Admin tier tool in a later version, or never?
- **Strict structure on edit** (12.4): bases carrying keys such as column widths cannot be edited until a sample of such a file is sent and the key is verified.
- **`map`** views: refused until their keys are verified. (`kanban` was allowed in 0.6.1 at the owner's request.)

### 12.10 Live results (11 November 2026, plugin 0.6.0, `writable_folders` = `Scratch`)

Run by a separate agent through the connector with the prompt for checklist steps 0 to 6, and relayed by the owner; the build session did not make these calls. What the build session checked itself afterwards, with read tools: `Scratch/Probe.base` exists (205 bytes), its stored text is the expected YAML (name `Probe2`, `limit: 5`), and the sha256 of that text is `835d18409ece67edd5c34324f324c36936a22c6dc848a414992e1a10c92a3ef8`, equal to the "after" hash the edit reported. The rest is as reported.

| Step | Reported result |
| --- | --- |
| 0 to 2 | `write_base`, `edit_base` present; vault 310 notes, 308 readable; gate `ok`, mode `hkdf`; `Index.base` parsed, 308 notes matched and 6 other files counted; no Medical path in the rows seen (first 5) |
| 3 | Created, `sha256 before: none`, 205 bytes; `read_base` parsed it; `query_base` listed the 4 `Test/Projects` notes sorted by `file.name` |
| 4 | Second write without `overwrite` refused, nothing written; with `overwrite=true` replaced, and the before hash equalled the earlier after hash |
| 5 | Valid edit applied; `kanban` refused ("type must be one of table, list, cards"); missing `old_text` refused |
| 6 | Seven writes refused with nothing stored: `Books/`, `Medical/` (a path that does not exist), `.obsidian`, `.md`, `.BASE`, invalid YAML, an unlisted view key |

Not verified: the Obsidian side (desktop and phone open the base, an edit made in Obsidian syncs back, no conflict file appears in `Scratch/`), checklist step 7 (deleting in Obsidian), Medical absence beyond the first rows of the `Index.base` query, and the oversize refusal for `Large.base`. `Scratch/Probe.base` is still in the vault and has to be deleted in Obsidian (no base delete tool).

**Obsidian side, first part (owner, 11 Nov 2026, 08:39 UTC):** the owner could see `Scratch/Probe.base` in Obsidian (device not stated; the phone was not confirmed) and added a `file.size` column there. The build session then read it back through the plugin: 223 bytes (18 more, the added line), `file.size` in `order`, `name: Probe2` and `limit: 5` unchanged, `parse: ok`; `query_base` evaluated it (4 notes, 3 other files counted, `file.size` values 17, 448, 152, 15). So an edit made in Obsidian does reach the database and is read back. `list_bases` and `list_notes` on `Scratch` showed one base and no notes, so no extra file appeared there; LiveSync conflicts live in document revisions and are not checked by this. Still not verified: that the rows Obsidian shows match the query (not compared), that the base opens on the phone, the other direction (an `edit_base` seen in Obsidian after a change made there), and deletion.

**Row comparison (owner's screenshot of Obsidian, 11 Nov 2026, desktop; shown "1/2", the second image has not been seen):** Obsidian's table for `Scratch/Probe.base` (view `Probe2`, filter `file.inFolder("Test")`, sort `file.name` ascending, limit 5) showed "5 results": Bork (17), Index.base (38), Large.base (2785), Link Forms (448), No Links (152), with columns name, path and file size. Compared with `query_base` (4 notes: Bork 17, Link Forms 448, No Links 152, Overview 15; "3 other files" counted):
- **Matches:** the notes shown, their paths, and their sizes; sort by name is case-insensitive and ignores the extension for ordering as the plugin does (Bork, Index.base, Large.base, Link Forms, No Links); `file.inFolder("Test")` includes sub-folders (the files are in `Test/Projects`); the 3 "other files" are the 3 bases under `Test` (38 and 2785 bytes agree with `list_bases`), so the count is right.
- **Differs, as 11.4 says it would:** Obsidian lists non-note files as rows and applies the view's limit over all files, so Index.base and Large.base took two of the 5 places and Overview and Search.base were cut off; the plugin lists notes only and applies the limit to notes, so it shows 4 notes including Overview.
- **New observation, unverified:** Obsidian showed the name of a base as `Index.base` (extension kept) and of a note as `Bork` (no extension). This may be only how the link in the cell is displayed, or it may mean `file.name` keeps the extension for non-notes. 0.5.1 uses the name without the extension for every file; the notes are verified (`zzqx`), non-notes are not. It affects only the count of "other files" when a filter compares `file.name`. To settle it the owner can change the filter of `Probe.base` to `file.name == "Index.base"` in Obsidian and report how many rows Obsidian shows; the plugin's count can then be compared.

### 12.11 Findings of 11 November 2026, and plugin 0.6.1

- **Phone:** the owner confirmed `Scratch/Probe.base` opens on the phone, and deleted it in Obsidian afterwards; `list_bases` on `Scratch` no longer listed it.
- **`file.name` of other files:** with the filter `file.name == "Index.base"` Obsidian showed 1 row, so a non-note keeps its extension in `file.name`. 0.6.1 does the same (notes unchanged). Tests: a unit test on both kinds of file; mutation checks made it fail when the name was always the basename and when it always had the extension. Effect: only the count of "other files".
- **Kanban:** the owner asked for a kanban base called `kanban` in `Scratch`. `Scratch/kanban.base` (155 bytes, 08:45 UTC) was already in the vault when the build session looked; the build session did not create it and does not know which tool made it, but its content is Obsidian's own shape: `type: kanban`, `name: kanban`, `groupBy` (`property: file.folder`, `direction: ASC`), `order` (`file.fullname`, `file.links`), and no other keys. That is a real sample, so 0.6.1 accepts `kanban` views, and requires `groupBy`. Not verified: the keys a kanban view may carry beyond these (for example how Obsidian stores card or column settings), so any other key is still refused.
- `write_base` and `edit_base` were not available to the build session, so the build session could not create a base itself in this session.

### 12.12 Second live run (11 November 2026, plugin 0.6.1, run by a separate agent, relayed by the owner)

The vault had changed since the first run: `Test/` (the four notes and the three bases), `Scratch/kanban.base` and four other notes were gone (306 notes, 304 readable; one base in the vault before this run, the blocked one) and `writable_folders` was now `scratch`, `claude` and `books`. The prompt's step 4 used the `Test/Projects` fixtures, so **the 0.6.1 `file.name` change was not tested live in this run**.

As reported: kanban creation passed (`Scratch/kanban2.base`, 156 bytes, read back as a kanban view grouped by `file.folder` ASC); a kanban without `groupBy`, a `map` view and an extra view key were each refused with nothing stored; overwrite is refused by default, a missing `old_text` is refused, an edit to an unsupported type is refused; `.obsidian`, `.md`, `.BASE` and the nonexistent `Medical/` path were refused. The agent did not run the `Books/` refusal because `books` is now writable (correct under the safety rules). Four of the reported hashes were not checkable, but the build session checked after the run with read tools: `Scratch/kanban2.base` and `Scratch/NameCheck.base` hold the expected text, and their sha256 are `deb94985…6946` (156 bytes) and `7087c962…1059` (139 bytes, the filter left by the last edit), equal to the hashes the agent reported for the creation and for the last edit.

Not verified: how Obsidian shows the kanban base (desktop and phone), the 0.6.1 `file.name` rule for non-notes against Obsidian, and any sync or conflict effect. A test that needs no `Test/` fixture: in Obsidian set the filter of `Scratch/NameCheck.base` to `file.name == "kanban2.base"` (a non-note file that exists now), read the number of rows Obsidian shows (expect 1), then ask for `query_base` on it (expect 0 notes and "1 other files").
