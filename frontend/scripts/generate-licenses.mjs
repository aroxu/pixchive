import { mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const lock = JSON.parse(
  await readFile(resolve(root, "package-lock.json"), "utf8"),
);
const sections = [
  "Third-party notices for Pixchive",
  "Generated from installed runtime dependencies. Each package retains its own license.",
];
let count = 0;

for (const [path, entry] of Object.entries(lock.packages).sort(([a], [b]) =>
  a.localeCompare(b, "en"),
)) {
  if (!path.startsWith("node_modules/") || entry.dev) continue;
  const folder = resolve(root, path);
  const metadata = JSON.parse(
    await readFile(resolve(folder, "package.json"), "utf8"),
  );
  const licenseFiles = (await readdir(folder, { withFileTypes: true }))
    .filter(
      (file) =>
        file.isFile() && /^(license|licence|copying|ofl)/i.test(file.name),
    )
    .map((file) => file.name)
    .sort();
  sections.push(`\n${"=".repeat(72)}\n${metadata.name} ${metadata.version}`);
  sections.push(
    `Declared license: ${metadata.license ?? entry.license ?? "See package source"}`,
  );
  for (const name of licenseFiles) {
    sections.push(
      `\n${name}\n${await readFile(resolve(folder, name), "utf8")}`,
    );
  }
  if (!licenseFiles.length) {
    if (metadata.name === "client-only") {
      // This React marker package ships no LICENSE, but shares React's license.
      sections.push(
        await readFile(resolve(root, "node_modules/react/LICENSE"), "utf8"),
      );
    } else {
      throw new Error(`Missing license text for ${metadata.name}`);
    }
  }
  count++;
}

const destination = resolve(
  root,
  process.argv[2] ?? "dist",
  "assets/third-party-licenses.txt",
);
await mkdir(dirname(destination), { recursive: true });
await writeFile(destination, `${sections.join("\n")}\n`);
console.log(`Wrote license notices for ${count} runtime dependencies.`);
