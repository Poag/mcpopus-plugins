# Third-party notices

The `obsidian` plugin is a new Python implementation. It reuses knowledge and behaviour from these MIT-licensed projects:

- **es617/obsidian-sync-mcp** (Copyright (c) 2026 Enrico Santagati and contributors, MIT): the tool names and output
  conventions, the note-path validation rules (`src/note-path.ts`), and the tag, link and frontmatter extraction rules
  (`src/parse.ts`) were reimplemented in Python from reading its source.
- **vrtmrz/livesync-commonlib** and **vrtmrz/octagonal-wheels** (Copyright (c) 2022 and 2024 vorotamoroz, MIT): the
  document formats, the encryption schemes and the compression format were reimplemented in Python from reading their
  source. `tests/test_crypto_vectors.py` contains test vectors copied from octagonal-wheels' test suite.
  `tools/gen_fixtures.mjs` runs octagonal-wheels 0.1.47 and fflate 0.8.2 (MIT) in CI to generate fixtures; neither is
  part of the plugin at run time.

No code was copied from these projects other than the test vectors.

This repository is itself MIT-licensed: see LICENSE.
