// Builds the compact pages the browser receives: paysignal/web/min/*.html
// Run after editing any page in paysignal/web:   node tools/minify_web.mjs
// Scripts and styles are minified with esbuild, HTML comments and indentation are removed.
// min/manifest.json records each source page's SHA-256; the server only serves a compact page whose
// hash matches the current source, so a page edited without rebuilding falls back to the readable one.
import { createRequire } from "module";
import { createHash } from "crypto";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const require = createRequire(import.meta.url);
let esbuild;
for (const p of ["esbuild", "/opt/npm-tools/node_modules/esbuild"]) {
  try { esbuild = require(p); break; } catch {}
}
if (!esbuild) { console.error("esbuild not found: npm i -D esbuild"); process.exit(1); }

const root = path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "paysignal", "web");
const out = path.join(root, "min");
fs.mkdirSync(out, { recursive: true });
const manifest = {};

function minifyHtml(src) {
  const kept = [];
  const keep = (s) => `\u0000${kept.push(s) - 1}\u0000`;
  let html = src.replace(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi, (m, attrs, body) => {
    if (/\bsrc=/i.test(attrs) || !body.trim()) return keep(m);
    if (/application\/(ld\+)?json/i.test(attrs)) return keep(`<script${attrs}>${JSON.stringify(JSON.parse(body))}</script>`);
    const code = esbuild.transformSync(body, { loader: "js", minify: true, target: "es2020", legalComments: "none" }).code.trim();
    if (/<\/script/i.test(code)) throw new Error("minified script contains </script");
    return keep(`<script${attrs}>${code}</script>`);
  });
  html = html.replace(/<style\b([^>]*)>([\s\S]*?)<\/style>/gi, (m, attrs, body) =>
    keep(`<style${attrs}>${esbuild.transformSync(body, { loader: "css", minify: true }).code.trim()}</style>`));
  html = html.replace(/<!--(?!\[if)[\s\S]*?-->/g, "");      // comments (not conditional ones)
  html = html.replace(/[ \t]*\n\s*/g, "\n").replace(/[ \t]{2,}/g, " ");
  return html.replace(/\u0000(\d+)\u0000/g, (_, i) => kept[+i]).trim() + "\n";
}

let before = 0, after = 0;
for (const name of fs.readdirSync(root).filter((n) => n.endsWith(".html")).sort()) {
  const src = fs.readFileSync(path.join(root, name), "utf8");
  const min = minifyHtml(src);
  fs.writeFileSync(path.join(out, name), min);
  manifest[name] = createHash("sha256").update(src, "utf8").digest("hex");
  before += Buffer.byteLength(src); after += Buffer.byteLength(min);
  console.log(`${name.padEnd(20)} ${String(Buffer.byteLength(src)).padStart(7)} -> ${String(Buffer.byteLength(min)).padStart(7)}`);
}
fs.writeFileSync(path.join(out, "manifest.json"), JSON.stringify(manifest, null, 1) + "\n");
console.log(`total ${before} -> ${after} bytes`);
