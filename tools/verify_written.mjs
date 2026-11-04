// Decrypts, with the real LiveSync code (octagonal-wheels 0.1.47), the chunks that the Python plugin wrote
// (tests/fixtures/written_by_plugin.json, produced by tests/test_interop.py::test_write_fixture_for_the_javascript_check).
// Exits non-zero if any chunk does not decrypt to the text the plugin meant to store.
import { readFileSync } from "node:fs";
import * as hkdf from "octagonal-wheels/encryption/hkdf";

const file = new URL("../tests/fixtures/written_by_plugin.json", import.meta.url).pathname;
const { passphrase, pbkdf2salt, chunks } = JSON.parse(readFileSync(file, "utf8"));
const salt = new Uint8Array(Buffer.from(pbkdf2salt, "base64"));
let bad = 0;
for (const c of chunks) {
  if (!c.id.startsWith("h:+") || c.e_ !== true || !c.data.startsWith("%=")) {
    console.error("not an encrypted LiveSync chunk:", c.id);
    bad++;
    continue;
  }
  const plain = await hkdf.decrypt(c.data, passphrase, salt);
  if (plain !== c.expected) {
    console.error("MISMATCH in", c.id);
    bad++;
  }
}
if (bad || chunks.length < 2) {
  console.error(`${bad} problem(s) in ${chunks.length} chunks`);
  process.exit(1);
}
console.log(`real LiveSync code decrypted all ${chunks.length} chunks written by the plugin`);
