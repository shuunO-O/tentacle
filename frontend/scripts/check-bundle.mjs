import fs from "node:fs";
import path from "node:path";

const ALLOWED_SUFFIXES = new Set([
  ".js",
  ".css",
  ".html",
  ".svg",
  ".png",
  ".ico",
  ".woff",
  ".woff2",
]);

const outDir = path.resolve(process.cwd(), process.env.TENTACLE_OUT_DIR || "../web/static");
if (!fs.existsSync(outDir)) {
  console.error(`Build output directory not found: ${outDir}`);
  process.exit(1);
}

let count = 0;
let totalBytes = 0;

function walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isSymbolicLink()) {
      console.error(`Symlink not allowed in build output: ${full}`);
      process.exit(1);
    }
    if (entry.isDirectory()) {
      walk(full);
    } else if (entry.isFile()) {
      const ext = path.extname(entry.name).toLowerCase();
      if (!ALLOWED_SUFFIXES.has(ext)) {
        console.error(`Disallowed suffix "${ext}" for deploy.CODE_SUFFIXES: ${full}`);
        process.exit(1);
      }
      count += 1;
      totalBytes += fs.statSync(full).size;
    }
  }
}

walk(outDir);
if (count === 0 || count > 256 || totalBytes > 16 * 1024 * 1024) {
  console.error(`Unexpected bundle metrics: count=${count}, bytes=${totalBytes}`);
  process.exit(1);
}
console.log(`Bundle verified cleanly: ${count} files, ${(totalBytes / 1024).toFixed(1)} KiB in ${outDir}`);
