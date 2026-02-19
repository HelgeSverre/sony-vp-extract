#!/usr/bin/env bun
import { createDecipheriv } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { basename, join } from "node:path";
// @ts-ignore
import LZMA from "lzma";
import chalk from "chalk";

const KEY = Buffer.from("eibohjeCh6uegahf");
const IV = Buffer.from("miefeinuShu9eilo");

const VOICE_PACK_URLS: Record<string, string> = {
  english: "https://info.update.sony.net/HP002/VGIDLPB0401/contents/0002/VP_english_UPG_03.bin",
  french: "https://info.update.sony.net/HP002/VGIDLPB0402/contents/0002/VP_french_UPG_03.bin",
  german: "https://info.update.sony.net/HP002/VGIDLPB0403/contents/0002/VP_german_UPG_03.bin",
  spanish: "https://info.update.sony.net/HP002/VGIDLPB0404/contents/0002/VP_spanish_UPG_03.bin",
  italian: "https://info.update.sony.net/HP002/VGIDLPB0405/contents/0002/VP_italian_UPG_03.bin",
  portuguese: "https://info.update.sony.net/HP002/VGIDLPB0406/contents/0002/VP_portuguese_UPG_03.bin",
  dutch: "https://info.update.sony.net/HP002/VGIDLPB0407/contents/0002/VP_dutch_UPG_03.bin",
  swedish: "https://info.update.sony.net/HP002/VGIDLPB0408/contents/0002/VP_swedish_UPG_03.bin",
  finnish: "https://info.update.sony.net/HP002/VGIDLPB0409/contents/0002/VP_finnish_UPG_03.bin",
  turkish: "https://info.update.sony.net/HP002/VGIDLPB0410/contents/0002/VP_turkish_UPG_03.bin",
};

function decrypt(body: Buffer): Buffer {
  const decipher = createDecipheriv("aes-128-cbc", KEY, IV);
  decipher.setAutoPadding(false);
  return Buffer.concat([decipher.update(body), decipher.final()]);
}

function decompressLzma(data: Buffer): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    LZMA.decompress(Array.from(data), (result: any, error: any) => {
      if (error) return reject(new Error(String(error)));
      resolve(Buffer.from(result));
    });
  });
}

function parseHeader(data: Buffer, filename: string) {
  const language = basename(filename).replace(/^VP_/, "").replace(/_UPG_\d+\.bin$/, "");
  return {
    language,
    fileSize: data.length,
    bodySize: data.readUInt32LE(0x10a),
    compressionType: data[0x104],
    decompressedSize: data.readUInt32LE(0x13a),
    destAddress: data.readUInt32LE(0x13e),
  };
}

async function extractPrompts(data: Buffer) {
  const decrypted = decrypt(data.subarray(0x1000));
  const decompressed = await decompressLzma(decrypted);

  const numEntries = decompressed.readUInt32LE(4);
  const tableEnd = 8 + numEntries * 8;
  const baseOffset = decompressed.readUInt32LE(12) - tableEnd;

  const prompts: { index: number; size: number; data: Buffer }[] = [];
  for (let i = 0; i < numEntries; i++) {
    const off = 8 + i * 8;
    const size = decompressed.readUInt32LE(off);
    const fileOffset = decompressed.readUInt32LE(off + 4) - baseOffset;
    if (fileOffset < 0 || fileOffset + size > decompressed.length) continue;
    prompts.push({ index: i, size, data: decompressed.subarray(fileOffset, fileOffset + size) });
  }
  return prompts;
}

function fmt(bytes: number) {
  return bytes >= 1024 * 1024
    ? `${(bytes / 1024 / 1024).toFixed(1)} MB`
    : `${(bytes / 1024).toFixed(1)} KB`;
}

// --- commands ---

async function showInfo(filepath: string) {
  const data = readFileSync(filepath);
  const info = parseHeader(data, filepath);
  const prompts = await extractPrompts(data);

  console.log();
  console.log(chalk.bold(basename(filepath)));
  console.log(chalk.dim("─".repeat(44)));
  console.log(`  language        ${chalk.cyan(info.language)}`);
  console.log(`  file size       ${fmt(info.fileSize)}`);
  console.log(`  encryption      ${chalk.yellow("AES-128-CBC + LZMA")}`);
  console.log(`  decompressed    ${fmt(info.decompressedSize)}`);
  console.log(`  prompts         ${chalk.green(String(prompts.length))}`);
  console.log();

  console.log(chalk.dim("  #     size        format"));
  console.log(chalk.dim("  " + "─".repeat(30)));
  for (const p of prompts) {
    const isId3 = p.data[0] === 0x49 && p.data[1] === 0x44 && p.data[2] === 0x33;
    const isMp3 = p.data[0] === 0xff && (p.data[1] & 0xe0) === 0xe0;
    const tag = isId3 ? chalk.dim("ID3+MP3") : isMp3 ? chalk.dim("MP3") : chalk.dim("audio");
    console.log(`  ${chalk.dim(String(p.index).padStart(2))}    ${fmt(p.size).padEnd(10)}  ${tag}`);
  }
  console.log();
}

async function extractFile(filepath: string, outputDir: string) {
  const data = readFileSync(filepath);
  const info = parseHeader(data, filepath);
  const langDir = join(outputDir, info.language);
  mkdirSync(langDir, { recursive: true });

  const prompts = await extractPrompts(data);
  for (const p of prompts) {
    writeFileSync(join(langDir, `prompt_${String(p.index).padStart(2, "0")}.mp3`), p.data);
  }

  const total = prompts.reduce((s, p) => s + p.size, 0);
  console.log(`  ${chalk.green("ok")}  ${info.language.padEnd(12)} ${chalk.dim(`${prompts.length} prompts, ${fmt(total)}`)}`);
}

async function downloadAll(outputDir: string) {
  mkdirSync(outputDir, { recursive: true });
  console.log(chalk.dim(`\ndownloading voice packs from Sony CDN...\n`));

  for (const [lang, url] of Object.entries(VOICE_PACK_URLS)) {
    const filename = basename(url);
    const outPath = join(outputDir, filename);
    if (existsSync(outPath) && statSync(outPath).size > 0) {
      console.log(`  ${chalk.dim("--")}  ${filename} ${chalk.dim("(cached)")}`);
      continue;
    }
    process.stdout.write(`  ${chalk.yellow("..")}  ${filename}`);
    const res = await fetch(url);
    if (!res.ok) {
      console.log(chalk.red(` failed (${res.status})`));
      continue;
    }
    writeFileSync(outPath, Buffer.from(await res.arrayBuffer()));
    console.log(chalk.dim(` ${fmt(statSync(outPath).size)}`));
  }
  console.log();
}

async function ensureVoicePacks(dir: string) {
  mkdirSync(dir, { recursive: true });
  if (readdirSync(dir).some((f) => f.endsWith(".bin"))) return;
  console.log(chalk.dim(`no .bin files in ${dir}/, fetching from CDN...`));
  await downloadAll(dir);
}

async function extractAll(inputDir: string, outputDir: string) {
  await ensureVoicePacks(inputDir);
  const files = readdirSync(inputDir).filter((f) => f.endsWith(".bin")).sort();
  if (files.length === 0) {
    console.error(chalk.red("no .bin files found"));
    process.exit(1);
  }

  console.log(chalk.bold(`\nsony-vp-extract`));
  console.log(chalk.dim(`${files.length} voice packs -> ${outputDir}/\n`));

  for (const f of files) {
    await extractFile(join(inputDir, f), outputDir);
  }
  console.log(chalk.dim(`\ndone.\n`));
}

// --- cli ---

const args = process.argv.slice(2);

if (args.length === 0 || args.includes("--help") || args.includes("-h")) {
  console.log(`
${chalk.bold("sony-vp-extract")} - Sony WH-1000XM4 voice pack extractor

${chalk.dim("usage:")}
  bun run cli/extract.ts ${chalk.cyan("<file.bin>")} ${chalk.dim("[output-dir]")}
  bun run cli/extract.ts ${chalk.cyan("--all")} ${chalk.dim("[input-dir] [output-dir]")}
  bun run cli/extract.ts ${chalk.cyan("--info")} ${chalk.dim("<file.bin>")}
  bun run cli/extract.ts ${chalk.cyan("--download")} ${chalk.dim("[output-dir]")}

${chalk.dim("examples:")}
  bun run cli/extract.ts voice-packs/VP_english_UPG_03.bin extracted/
  bun run cli/extract.ts --all voice-packs/ extracted/
  bun run cli/extract.ts --all
`);
  process.exit(0);
}

if (args[0] === "--info") {
  await showInfo(args[1]);
} else if (args[0] === "--download") {
  await downloadAll(args[1] || "voice-packs");
} else if (args[0] === "--all") {
  await extractAll(args[1] || "voice-packs", args[2] || "extracted");
} else {
  await extractFile(args[0], args[1] || "extracted");
}

process.exit(0);
