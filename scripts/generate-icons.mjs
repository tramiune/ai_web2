import { writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import sharp from "sharp";
import toIco from "to-ico";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "public");
const src = join(root, "logo.png");

const pngSizes = [16, 32, 48, 64, 180, 192, 512];
const resizeOpts = {
  fit: "contain",
  background: { r: 0, g: 0, b: 0, alpha: 0 },
};

for (const size of pngSizes) {
  await sharp(src)
    .resize(size, size, resizeOpts)
    .png({ compressionLevel: 9 })
    .toFile(join(root, `icon-${size}.png`));
  console.log(`icon-${size}.png`);
}

await sharp(src)
  .resize(32, 32, resizeOpts)
  .png({ compressionLevel: 9 })
  .toFile(join(root, "favicon.png"));
console.log("favicon.png");

const icoBuffers = await Promise.all(
  [16, 32, 48].map((size) =>
    sharp(src).resize(size, size, resizeOpts).png().toBuffer()
  )
);
await writeFile(join(root, "favicon.ico"), await toIco(icoBuffers));
console.log("favicon.ico");
