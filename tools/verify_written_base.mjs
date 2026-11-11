// Reads back, with the real LiveSync code (octagonal-wheels 0.1.47, xxhash-wasm), the .base documents that the Python plugin wrote
// (tests/fixtures/written_base_by_plugin.json, produced by tests/test_interop.py::test_write_base_fixture_for_the_javascript_check).
// Checks, for a plain and an encrypted vault: the entry is a "newnote" with the byte size; every chunk decodes (decrypted first when
// encrypted) through the library's own decodeBinary to exactly the bytes the plugin meant to store; unencrypted chunk ids are the ones
// the library computes (h64(`${data}-${data.length}`).toString(36)); encrypted chunks are HKDF "%=" with the "h:+" prefix.
// Exits non-zero on any difference.
import { readFileSync } from "node:fs";
import * as hkdf from "octagonal-wheels/encryption/hkdf";
import { decodeBinary } from "octagonal-wheels/binary";
import xxhash from "xxhash-wasm";

const file = new URL("../tests/fixtures/written_base_by_plugin.json", import.meta.url).pathname;
const { passphrase, text, plain, encrypted } = JSON.parse(readFileSync(file, "utf8"));
const expected = new TextEncoder().encode(text);
const xx = await xxhash();
let bad = 0;
const fail = (m) => { console.error(m); bad++; };

function same(a, b) {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

for (const [label, entry] of [["plain", plain], ["encrypted", encrypted]]) {
  const { meta, chunks } = entry;
  if (meta.type !== "newnote") fail(`${label}: type is ${meta.type}, not newnote`);
  if (meta.size !== expected.length) fail(`${label}: size ${meta.size} is not the byte length ${expected.length}`);
  if (JSON.stringify(meta.children) !== JSON.stringify(chunks.map((c) => c.id))) fail(`${label}: children do not list the chunks in order`);
  if (Object.keys(meta.eden).length !== 0) fail(`${label}: eden is not empty`);
  const stored = [];
  for (const c of chunks) {
    if (label === "plain") {
      if (c.e_) fail(`${label}: chunk ${c.id} is marked encrypted`);
      const id = "h:" + xx.h64(`${c.data}-${c.data.length}`).toString(36);
      if (c.id !== id) fail(`${label}: chunk id ${c.id} is not the library's id ${id}`);
      stored.push(c.data);
    } else {
      if (!c.id.startsWith("h:+") || c.e_ !== true || !c.data.startsWith("%=")) { fail(`${label}: chunk ${c.id} is not an HKDF chunk`); continue; }
      const salt = new Uint8Array(Buffer.from(entry.pbkdf2salt, "base64"));
      stored.push(await hkdf.decrypt(c.data, passphrase, salt));
    }
  }
  // the library's own reader of binary entries: every chunk is base64, the list is decoded together
  const bytes = new Uint8Array(decodeBinary(stored));
  if (!same(bytes, expected)) fail(`${label}: the bytes read back with decodeBinary differ from what the plugin meant to store`);
  // each chunk also decodes on its own (readers may do either)
  const parts = stored.map((s) => new Uint8Array(decodeBinary(s)));
  const joined = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) { joined.set(p, at); at += p.length; }
  if (!same(joined, expected)) fail(`${label}: chunks decoded one by one differ`);
  if (new TextDecoder("utf-8", { fatal: true }).decode(bytes) !== text) fail(`${label}: the text differs`);
  console.log(`${label}: ${chunks.length} chunks, ${bytes.length} bytes read back by the real LiveSync code`);
}
if (bad) { console.error(`${bad} problem(s)`); process.exit(1); }
