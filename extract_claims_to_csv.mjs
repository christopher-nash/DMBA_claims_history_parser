#!/usr/bin/env node
/**
 * Medical Claims (EOB) PDF → CSV extractor (DMBA-style) using pdfjs-dist.
 *
 * This version disables font loading (disableFontFace: true) to avoid font warnings.
 * That’s safe because we only need text content, not font rendering.
 *
 * Usage:
 *   node extract_claims_to_csv.mjs PrintFriendlyEOB.pdf history.csv
 *
 * Deps:
 *   npm i pdfjs-dist
 */

import fs from "fs";
import path from "path";
import * as pdfjsLib from "pdfjs-dist/legacy/build/pdf.mjs";

// ------------- CLI -------------
if (process.argv.length < 4) {
  console.error("Usage: node extract_claims_to_csv.mjs <input.pdf> <output.csv>");
  process.exit(1);
}
const inputPdf = process.argv[2];
const outputCsv = process.argv[3];

// Disable web worker in Node and quiet logs
pdfjsLib.GlobalWorkerOptions.disableWorker = true;
if (pdfjsLib.setVerbosityLevel && pdfjsLib.VerbosityLevel) {
  pdfjsLib.setVerbosityLevel(pdfjsLib.VerbosityLevel.ERROR);
}

// ------------- Helpers -------------
function normalizeSpacesKeepNewlines(s) {
  return s
    .replace(/\u00a0/g, " ")
    .split("\n")
    .map((ln) => ln.replace(/[ \t]{2,}/g, " ").trimEnd())
    .join("\n");
}

function toCsvLine(values) {
  return values
    .map((v) => {
      const s = String(v ?? "");
      return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    })
    .join(",");
}

function writeCsv(rows, outPath) {
  const headers = [
    "Claim", "Patient", "Health Plan", "Participant", "Participant Id",
    "Date Entered", "Date Paid", "Provider",
    "Service Date", "Services Provided",
    "Provider Billed ($)", "DMBA Paid ($)", "Your Responsibility ($)",
    "Message Codes",
  ];
  const out = [toCsvLine(headers)]
    .concat(rows.map((r) => toCsvLine(headers.map((h) => r[h] ?? ""))))
    .join("\n") + "\n";
  fs.writeFileSync(outPath, out, "utf8");
}

function moneyToStr(val) {
  if (!val) return "";
  let s = val.replace(/,/g, "").trim();
  if (s.startsWith("$")) s = s.slice(1);
  const num = Number.parseFloat(s);
  return Number.isFinite(num) ? num.toFixed(2) : s;
}

// ---- Patterns tuned to this EOB layout ----
const CURRENCY = String.raw`(?:\$?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d{2})?)`;

const SERVICE_ROW_RE = new RegExp(
  String.raw`^` +
    String.raw`(?<ServiceDate>\d{2}\/\d{2}\/\d{4})\s+` +   // date
    String.raw`(?<Service>.*?)\s+` +                       // desc (lazy)
    String.raw`(?<ProviderBilled>${CURRENCY})\s+` +
    String.raw`(?<DMBA_Paid>${CURRENCY})\s+` +
    String.raw`(?<YourResp>${CURRENCY})\s+` +
    String.raw`(?<MessageCodes>[A-Z0-9 ]{1,40})` +
  String.raw`$`
);

const HEADER_PATTERNS = [
  ["Claim",          /\bClaim\s*:?\s*(T\d{7,})/s],
  ["Patient",        /\bPatient\s+(.+?)\s+Health\s*Plan\b/s],
  ["Health Plan",    /\bHealth\s*Plan\s+(.+?)\s+(?:Participant|Date\s*Entered)\b/s],
  ["Participant",    /\bParticipant\s+(.+?)\s+Date\s*Entered\b/s],
  ["Participant Id", /\bParticipant\s*Id\s+([0-9]+)\b/s],
  ["Date Entered",   /\bDate\s*Entered\s+(\d{2}\/\d{2}\/\d{4})\b/s],
  ["Date Paid",      /\bDate\s*Paid\s+(\d{2}\/\d{2}\/\d{4})\b/s],
  ["Provider",       /\bProvider\s+(.+?)(?:\n|$)/s],
];

function extractHeaderFields(pageText) {
  const header = {};
  for (const [key, rx] of HEADER_PATTERNS) {
    const m = pageText.match(rx);
    header[key] = m && m[1] ? m[1].replace(/\s+/g, " ").trim() : "";
  }
  return header;
}

function extractServiceLines(pageText) {
  const rows = [];
  for (const raw of pageText.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const m = line.match(SERVICE_ROW_RE);
    if (m?.groups) {
      rows.push({
        "Service Date": m.groups.ServiceDate,
        "Services Provided": m.groups.Service.trim(),
        "Provider Billed ($)": moneyToStr(m.groups.ProviderBilled),
        "DMBA Paid ($)": moneyToStr(m.groups.DMBA_Paid),
        "Your Responsibility ($)": moneyToStr(m.groups.YourResp),
        "Message Codes": m.groups.MessageCodes.trim(),
      });
    }
  }
  return rows;
}

/** Rebuild lines from pdf.js text items by grouping items with similar Y and sorting by X. */
function pageTextFromItems(textContent, { yRound = 1, sortY = "desc" } = {}) {
  const linesByY = new Map(); // y -> [{x,str},...]
  for (const it of textContent.items) {
    const [,, , , x, y] = it.transform; // a,b,c,d,e(x),f(y)
    const yKey = yRound > 0 ? Math.round(y / yRound) * yRound : Math.round(y);
    const arr = linesByY.get(yKey) || [];
    arr.push({ x, str: it.str });
    linesByY.set(yKey, arr);
  }
  const ys = Array.from(linesByY.keys()).sort((a, b) => (sortY === "asc" ? a - b : b - a));
  const lines = ys.map((y) =>
    linesByY.get(y).sort((p, q) => p.x - q.x).map((p) => p.str).join(" ").replace(/\s{2,}/g, " ").trim()
  );
  return lines.join("\n");
}

// ------------- Main -------------
(async function main() {
  try {
    const data = new Uint8Array(fs.readFileSync(path.resolve(inputPdf)));

    const loadingTask = pdfjsLib.getDocument({
      data,
      isEvalSupported: false,
      disableFontFace: true,     // <-- key: do not load fonts (no warnings)
      cMapPacked: true,          // fine to keep; we don't set cMapUrl -> built-ins
    });
    const doc = await loadingTask.promise;

    const allRows = [];

    for (let i = 1; i <= doc.numPages; i++) {
      const page = await doc.getPage(i);
      const content = await page.getTextContent({ normalizeWhitespace: true });
      const pageRaw = pageTextFromItems(content, { yRound: 1, sortY: "desc" });
      const pageText = normalizeSpacesKeepNewlines(pageRaw);

      const header = extractHeaderFields(pageText);
      const serviceRows = extractServiceLines(pageText);

      for (const r of serviceRows) {
        allRows.push({
          "Claim": header["Claim"] || "",
          "Patient": header["Patient"] || "",
          "Health Plan": header["Health Plan"] || "",
          "Participant": header["Participant"] || "",
          "Participant Id": header["Participant Id"] || "",
          "Date Entered": header["Date Entered"] || "",
          "Date Paid": header["Date Paid"] || "",
          "Provider": header["Provider"] || "",
          ...r,
        });
      }
    }

    writeCsv(allRows, path.resolve(outputCsv));
    console.log(`Extracted ${allRows.length} service line(s) to: ${outputCsv}`);
  } catch (err) {
    console.error("Error:", err);
    process.exit(1);
  }
})();

