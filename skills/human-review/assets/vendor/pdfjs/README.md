# Vendored PDF.js

`pdf.min.mjs` and `pdf.worker.min.mjs` are copied, byte for byte, from the official
`pdfjs-dist` package. They are the only third-party code in this skill, and they never leave the
machine: the review server reads them from this folder and serves them from `127.0.0.1`.

- **Project:** [mozilla/pdf.js](https://github.com/mozilla/pdf.js)
- **Package:** [`pdfjs-dist`](https://www.npmjs.com/package/pdfjs-dist)
- **Version:** 6.3.289, pinned exactly
- **Tarball:** <https://registry.npmjs.org/pdfjs-dist/-/pdfjs-dist-6.3.289.tgz>
- **Tarball SHA-256:** `06f25e887adc6489f04c9fcb14198c77e4e5623a59a0bba5c4cea5838a4f1241`
- **Tarball SHA-512, base64,** the `dist.integrity` npm publishes:
  `ZHjSVpDa3D6izMq8/04lvkhkATUmL9px6ChPaXc1k6nU2Mrhlg1/7F0bdUqCwUjw3NsPTfPZsMDUU6ZIcRaeQw==`
- **License:** Apache-2.0, the same license as this repository. The full text is in
  [`LICENSE`](LICENSE), copied from the package.
- **Copied from the tarball:** `package/build/pdf.min.mjs`, `package/build/pdf.worker.min.mjs`,
  `package/LICENSE`. Nothing was edited, and `SHA256SUMS` proves it.

Per-file SHA-256 is in [`SHA256SUMS`](SHA256SUMS), and `bin/review selftest` verifies every file
against it on every run. A file that does not match fails the test rather than reaching a browser.

## What is deliberately NOT vendored

- **The wasm decoders** (JPEG 2000, JBIG2). A scanned PDF that uses them shows a blank page with a
  message instead. They are shipped bytecode, which neither this repository nor the skill scanner
  wants.
- **The cmaps** (CJK encodings) and the **standard font data**. A PDF that names Helvetica, Times or
  Courier without embedding it is drawn with the matching font of the machine, which is what every
  other viewer on that machine does too. A CJK PDF without embedded fonts can show empty glyphs.
- **`pdf_viewer.css`.** The page carries its own copy of the `.textLayer` rules that PDF.js needs,
  about 20 lines, marked as such in `assets/review.html`.
- **`pdf.sandbox.mjs`.** It exists to run the JavaScript inside a PDF. Nothing here enables
  scripting, so the file would be dead weight and a live risk.

## Version floor, and why this one

`6.3.289` is the newest release as of 2026-10-02. The floor is **6.2.108**: everything older carries
[CVE-2026-16633](https://advisories.gitlab.com/npm/pdfjs-dist/CVE-2026-16633/) (CVSS 8.1, arbitrary
JavaScript through `enableScripting`). Everything older than 4.2.67 also carries
[CVE-2024-4367](https://codeanlabs.com/2024/05/cve-2024-4367-arbitrary-js-execution-in-pdf-js/)
(arbitrary JavaScript through a crafted font matrix).

The page hardens both anyway, because a pinned copy is only as fresh as the last update:

- `isEvalSupported: false` on every `getDocument` call, which is the documented workaround for
  CVE-2024-4367.
- Scripting and XFA are never enabled (`enableScripting` and `enableXfa` stay off).
- The server sends a Content-Security-Policy with `default-src 'none'`, a per-response nonce for the
  two inline scripts, and `connect-src 'self'`. A PDF.js bug that reaches script execution still
  cannot call out, and no third-party host is reachable from the page at all.

## Update procedure

PDF.js publishes often, and a parser that reads untrusted documents is worth keeping current. Check
every few months, and at once when an advisory names `pdfjs-dist`.

```sh
V=<new version>                       # pick from https://registry.npmjs.org/pdfjs-dist
cd "$(mktemp -d)"
curl -sS -O "https://registry.npmjs.org/pdfjs-dist/-/pdfjs-dist-$V.tgz"
shasum -a 256 "pdfjs-dist-$V.tgz"     # record it in the table above
tar xzf "pdfjs-dist-$V.tgz" package/build/pdf.min.mjs package/build/pdf.worker.min.mjs package/LICENSE
D=<repo>/skills/human-review/assets/vendor/pdfjs
cp package/build/pdf.min.mjs package/build/pdf.worker.min.mjs package/LICENSE "$D/"
cd "$D" && shasum -a 256 pdf.min.mjs pdf.worker.min.mjs LICENSE > SHA256SUMS
```

Then, in this order:

1. Update the version, the tarball URL and both tarball hashes in the table above.
2. Run `bin/review selftest`. It re-verifies `SHA256SUMS` and the page's offline rule.
3. Run `bin/review selftest --browser`. It renders a generated PDF, reads its text layer back and
   cuts a crop, so a breaking API change fails here rather than in front of a reviewer.
4. Open a real multi-page PDF with `bin/review open`, on a desktop window and at a phone width.
5. Re-run `skillscan.py <skill> --mode author` and record the verdict.
6. Note the new version in `SKILL.md` under "Requirements and limits".

Check the [PDF.js release notes](https://github.com/mozilla/pdf.js/releases) for a removed export.
The page uses exactly four: `getDocument`, `GlobalWorkerOptions`, `TextLayer` and `PixelsPerInch`.
