// Generates test fixtures with the real LiveSync crypto code (octagonal-wheels 0.1.47, MIT), so the Python reader
// is checked against what LiveSync actually writes rather than against our reading of its source.
// Document shapes follow livesync-commonlib src/pouchdb/encryption.ts (incomingEncryptHKDF / incomingEncryptV1).
//
// Run (CI does this): cd tools && npm ci --ignore-scripts && node gen_fixtures.mjs
// Writes ../tests/fixtures/*.json (git-ignored). The passphrase below is a test value, not a secret.
import { writeFileSync, mkdirSync } from "node:fs";
import { createHash } from "node:crypto";
import { deflateSync } from "fflate";
import * as hkdf from "octagonal-wheels/encryption/hkdf";
import * as enc from "octagonal-wheels/encryption/encryption";
import { encryptV3 } from "octagonal-wheels/encryption/encryptionv3";
import { arrayBufferToBase64Single } from "octagonal-wheels/binary/base64";
import xxhash from "xxhash-wasm";

const OUT = new URL("../tests/fixtures/", import.meta.url).pathname;
mkdirSync(OUT, { recursive: true });
const PASS = "test-passphrase-not-a-secret";
const sha = (s) => createHash("sha256").update(s).digest("hex");
const NOTES = {
  "Claude/Code/demo.md":
    "---\ntitle: Demo\ntags: [group/code, mcp]\n---\n# Demo\nLinks to [[Other Note]] and [text](Claude/Tech/server.md). Inline #inline-tag here.\n" +
    "Line of filler text for chunking. ".repeat(40) + "\nEnd of demo.\n",
  "Claude/Tech/server.md": "# Server\nBack to [[demo]]. Unicode: café ☕ 日本語 🐙.\n",
  "Other Note.md": "Plain root note with a #root tag.\n",
  "Medical/Conditions/private.md": "# Private\nSensitive text that must never leave when Medical/ is blocked.\n",
  "Daily/2026-11-04.md": "---\ntags:\n  - daily\n---\nToday.\n",
};
const MTIME = 1_762_000_000_000;

function splitChunks(text, size = 300) {
  const out = [];
  for (let i = 0; i < text.length; i += size) out.push(text.slice(i, i + size));
  return out.length ? out : [""];
}
// "\u000eLZ\u001d" + base64(raw DEFLATE), as livesync-commonlib src/pouchdb/compress.ts writes it
const compressed = async (text) =>
  "\u000eLZ\u001d" + (await arrayBufferToBase64Single(deflateSync(new TextEncoder().encode(text), { level: 8 })));

async function build(mode) {
  const docs = [];
  const salt = hkdf.createPBKDF2Salt();
  const saltB64 = await arrayBufferToBase64Single(salt);
  let i = 0;
  for (const [path, text] of Object.entries(NOTES)) {
    i++;
    const chunks = splitChunks(text);
    const children = [];
    const eden = {};
    for (let c = 0; c < chunks.length; c++) {
      const id = (mode === "plain" ? "h:" : "h:+") + sha(path + c).slice(0, 24);
      const part = chunks[c];
      const doc = { _id: id, _rev: "1-" + sha(id).slice(0, 32), type: "leaf", data: part };
      if (mode === "plain") {
        if (i === 2) doc.data = await compressed(part); // compressed, not encrypted
        if (i === 3) doc.data = Buffer.from(part).toString("base64"); // binary-typed entry: base64 per chunk
      } else if (mode === "hkdf") {
        if (i === 1) doc.data = await hkdf.encrypt(await compressed(part), PASS, salt); // compress, then encrypt
        else if (i === 2) doc.data = await compressed(await hkdf.encrypt(part, PASS, salt)); // encrypt, then compress
        else if (i === 3) doc.data = await hkdf.encryptWithEphemeralSalt(part, PASS); // "%$": salt carried inline
        else doc.data = await hkdf.encrypt(part, PASS, salt);
        doc.e_ = true;
      } else {
        doc.data = i === 1 ? await enc.encryptV1(part, PASS, false) : i === 2 ? await encryptV3(part, PASS) : await enc.encrypt(part, PASS, false);
        doc.e_ = true;
      }
      if (i === 5 && c === 0 && mode !== "v1") {
        eden[id] = { data: part, epoch: 1 }; // eden carries cleartext chunks; encryption wraps the whole eden object
      } else {
        docs.push(doc);
      }
      children.push(id);
    }
    const meta = {
      _id: path.toLowerCase(), _rev: "1-" + sha(path).slice(0, 32), type: i === 3 && mode === "plain" ? "newnote" : "plain",
      path, children, ctime: MTIME + i - 5000, mtime: MTIME + i * 1000, size: text.length, eden,
    };
    if (mode === "hkdf") {
      meta._id = "f:" + sha(path);
      const props = JSON.stringify({ path, mtime: meta.mtime, ctime: meta.ctime, size: meta.size, children });
      meta.path = "/\\:" + (await hkdf.encrypt(props, PASS, salt));
      meta.mtime = 0; meta.ctime = 0; meta.size = 0; meta.children = [];
      if (Object.keys(eden).length) {
        meta.eden = { "h:++encrypted-hkdf": { data: await hkdf.encrypt(JSON.stringify(eden), PASS, salt), epoch: 999999 } };
      }
    } else if (mode === "v1") {
      meta._id = "f:" + sha(path);
      meta.path = await enc.obfuscatePath(path, PASS, false);
    }
    docs.push(meta);
  }
  // entries the reader must skip: a soft-deleted note, an internal file, a non-markdown file, control documents
  docs.push({ _id: "gone.md", _rev: "2-aa", type: "plain", path: "Gone.md", children: [], ctime: 1, mtime: 2, size: 0, deleted: true, eden: {} });
  docs.push({ _id: "i:.obsidian/app.json", _rev: "1-aa", type: "plain", path: "i:.obsidian/app.json", children: [], ctime: 1, mtime: 1, size: 2, eden: {} });
  docs.push({ _id: "image.png", _rev: "1-bb", type: "newnote", path: "image.png", children: [], ctime: 1, mtime: 1, size: 5, eden: {} });
  docs.push({ _id: "obsydian_livesync_version", _rev: "1-cc", type: "versioninfo", version: 12 });
  if (mode === "hkdf") docs.push({ _id: "_local/obsidian_livesync_sync_parameters", type: "sync-parameters", protocolVersion: 2, pbkdf2salt: saltB64 });
  return { mode, passphrase: mode === "plain" ? null : PASS, notes: NOTES, docs };
}

for (const mode of ["plain", "hkdf", "v1"]) {
  writeFileSync(`${OUT}vault_${mode}.json`, JSON.stringify(await build(mode), null, 1));
}

// Chunk ids exactly as livesync-commonlib's XXHash64HashManager computes them without encryption:
// h64(`${piece}-${piece.length}`).toString(36), where length is in UTF-16 code units.
const xx = await xxhash();
const pieces = ["helloWorld", "", "café ☕\n", "😀", "日本語\n", "a".repeat(1000), "line 😀 with astral\nnext line\n", "# Title\n"];
writeFileSync(`${OUT}chunk_ids.json`, JSON.stringify(pieces.map((p) => ({ piece: p, id: "h:" + xx.h64(`${p}-${p.length}`).toString(36) })), null, 1));

// One primitive vector per format.
const salt = hkdf.createPBKDF2Salt();
const text = "Supercalifragilistic ✨ 日本語 🐙 line\nsecond line";
writeFileSync(`${OUT}vectors.json`, JSON.stringify({
  passphrase: PASS, plaintext: text, pbkdf2salt: await arrayBufferToBase64Single(salt),
  hkdf: await hkdf.encrypt(text, PASS, salt),
  hkdf_salted: await hkdf.encryptWithEphemeralSalt(text, PASS),
  v3: await encryptV3(text, PASS),
  v2: await enc.encrypt(text, PASS, false),
  v2_dynamic: await enc.encrypt(text, PASS, true),
  v1: await enc.encryptV1(text, PASS, false),
  obfuscated_path: await enc.obfuscatePath("Folder/Note.md", PASS, false), path: "Folder/Note.md",
}, null, 1));
console.log("wrote fixtures to", OUT);
