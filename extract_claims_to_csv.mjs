#!/usr/bin/env node
/**
 * Medical Claims (EOB) PDF → CSV extractor (DMBA-style, print-friendly)
 * using pdfjs-dist, with multi-page claim support and legend-only page skipping.
 *
 * Features
 * --------
 * - Extracts header fields (Claim, Patient, Health Plan, Participant, Participant Id,
 *   Date Entered, Date Paid, Provider). If a claim spans multiple pages with no header
 *   on page 2+, we carry forward the last seen header context.
 * - Extracts service rows like: 02/05/2025 OFFICE VISIT $223.00 $0.00 $102.36 B6 N3
 * - Skips legend-only pages (e.g., pages that have "Code Description" + code lines but no services).
 * - V3 parity: negative/parenthesized currency, wrapped/multi-line rows, across-page row carry.
 *
 * Deps
 * ----
 * - Node 18+
 * - pdfjs-dist  (npm i pdfjs-dist)
 *
 * Usage
 * -----
 *   node extract_claims_to_csv_V3_handles_multi-pages.js PrintFriendlyEOB.pdf history.csv
 */

import fs from "fs";
import path from "path";
import * as pdfjsLib from "pdfjs-dist/legacy/build/pdf.mjs";

// ---------------- CLI ----------------
if (process.argv.length < 4) {
  console.error("Usage: node extract_claims_to_csv_V3_handles_multi-pages.js <input.pdf> <output.csv>");
  process.exit(1);
}
const inputPdf = process.argv[2];
const outputCsv = process.argv[3];

// Disable worker & font loading (we only need text; keeps the console quiet)
pdfjsLib.GlobalWorkerOptions.disableWorker = true;
if (pdfjsLib.setVerbosityLevel && pdfjsLib.VerbosityLevel) {
  pdfjsLib.setVerbosityLevel(pdfjsLib.VerbosityLevel.ERROR);
}

// ---------------- Helpers ----------------
function normalizeSpacesKeepNewlines(s) {
  return (s || "")
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
  const out =
    [toCsvLine(headers)]
      .concat(rows.map(r => toCsvLine(headers.map(h => r[h] ?? ""))))
      .join("\n") + "\n";
  fs.writeFileSync(outPath, out, "utf8");
}

// --- Money normalization (V3: supports negatives and parentheses) ---
function moneyToStr(val) {
  if (val == null) return "";
  let s = String(val).trim().replace(/,/g, "");
  // leading $ anywhere
  if (s.startsWith("$")) s = s.slice(1);
  // parentheses -> negative
  if (s.startsWith("(") && s.endsWith(")")) s = "-" + s.slice(1, -1);
  // stray dollar signs
  s = s.replace(/\$/g, "");
  const n = Number(s);
  return Number.isFinite(n) ? n.toFixed(2) : s;
}

// ---------- Patterns (mirrors the Python V3 script) ----------
const CURRENCY = String.raw`\(?-?\$?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d{2})?\)?`;

// final, single-line service row (after assembly)
const SERVICE_ROW_RE = new RegExp(
  String.raw`^`
  + String.raw`(?<ServiceDate>\d{2}\/\d{2}\/\d{4})\s+`
  + String.raw`(?<Service>.*?)\s+`
  + String.raw`(?<ProviderBilled>${CURRENCY})\s+`
  + String.raw`(?<DMBA_Paid>${CURRENCY})\s+`
  + String.raw`(?<YourResp>${CURRENCY})\s+`
  + String.raw`(?<MessageCodes>[A-Z0-9 ]{1,40})`
  + String.raw`$`
);

// helpers for assembly
const DATE_START_RE = /^\d{2}\/\d{2}\/\d{4}\b/;
const CURRENCY_ANY_RE = new RegExp(CURRENCY, "g");

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

const PAGE_FOOTER_RE = /\bPage\s+(\d+)\b/i;
const LEGEND_HDR_RE  = /\bCode\s+Description\b/i;
const LEGEND_LINE_RE = /^[A-Z0-9]{1,4}\b\s+.+/m;         // e.g., "B6 SERVICES WERE ..."
const TOTALS_RE      = new RegExp(`^Totals\\s+${CURRENCY}\\s+${CURRENCY}\\s+${CURRENCY}\\b`, "m");

// ---------- Extraction helpers ----------
function extractHeaderFields(pageText) {
  const header = {};
  for (const [key, rx] of HEADER_PATTERNS) {
    const m = pageText.match(rx);
    header[key] = m && m[1] ? m[1].replace(/\s+/g, " ").trim() : "";
  }
  return header;
}

// V3: assemble rows from lines (wraps + across pages)
function assembleRowsFromLines(lines, pending = null) {
  const rows = [];
  let buf = pending;

  const tryFinalize = (buffer) => {
    if (!buffer) return null;
    const currencyCount = (buffer.match(CURRENCY_ANY_RE) || []).length;
    if (currencyCount >= 3) {
      const oneLine = buffer.replace(/\s+/g, " ").trim();
      if (SERVICE_ROW_RE.test(oneLine)) return oneLine;
    }
    return null;
  };

  for (const raw of lines) {
    const line = (raw || "").trim();
    if (!line) continue;

    if (buf == null) {
      // only begin a row if the line starts with a date
      if (DATE_START_RE.test(line)) {
        buf = line;
        const done = tryFinalize(buf);
        if (done) {
          rows.push(done);
          buf = null;
        }
      }
    } else {
      // continue current buffer
      buf = `${buf} ${line}`;
      const done = tryFinalize(buf);
      if (done) {
        rows.push(done);
        buf = null;
      }
    }
  }

  return { completedRows: rows, pending: buf };
}

// turn a finalized (assembled) row string into an object
function parseServiceRow(row) {
  const m = row.match(SERVICE_ROW_RE);
  if (!m?.groups) throw new Error(`row did not match service pattern: ${row}`);
  return {
    "Service Date": m.groups.ServiceDate,
    "Services Provided": m.groups.Service.trim(),
    "Provider Billed ($)": moneyToStr(m.groups.ProviderBilled),
    "DMBA Paid ($)": moneyToStr(m.groups.DMBA_Paid),
    "Your Responsibility ($)": moneyToStr(m.groups.YourResp),
    "Message Codes": m.groups.MessageCodes.trim(),
  };
}

function pageFooterNumber(pageText) {
  // look at last few non-empty lines only
  const lines = pageText.split("\n").map(s => s.trim()).filter(Boolean);
  for (const ln of lines.slice(-5)) {
    const m = ln.match(PAGE_FOOTER_RE);
    if (m) {
      const n = Number(m[1]);
      if (Number.isFinite(n)) return n;
    }
  }
  return null;
}

function isLegendOnlyPage(pageText, numServiceRows) {
  if (numServiceRows > 0) return false;
  if (LEGEND_HDR_RE.test(pageText)) {
    // require at least a couple legend code lines to avoid false positives
    const matches = pageText.match(new RegExp(LEGEND_LINE_RE, "gm")) || [];
    if (matches.length >= 2) return true;
  }
  return false;
}

/** Group pdf.js text items by Y (rounded), sort by X, join into lines. */
function pageTextFromItems(textContent, { yRound = 1, sortY = "desc" } = {}) {
  const linesByY = new Map(); // yKey -> [{x, str}, ...]
  for (const it of textContent.items) {
    const [,,, , x, y] = it.transform; // a,b,c,d,e(x),f(y)
    const yKey = yRound > 0 ? Math.round(y / yRound) * yRound : Math.round(y);
    const bucket = linesByY.get(yKey) || [];
    bucket.push({ x, str: it.str });
    linesByY.set(yKey, bucket);
  }
  const ys = Array.from(linesByY.keys()).sort((a, b) => (sortY === "asc" ? a - b : b - a));
  const lines = ys.map(y => {
    const parts = linesByY.get(y).sort((p, q) => p.x - q.x);
    return parts.map(p => p.str).join(" ").replace(/\s{2,}/g, " ").trim();
  });
  return lines.join("\n");
}

// ---------------- Main ----------------
(async function main() {
  try {
    const data = new Uint8Array(fs.readFileSync(path.resolve(inputPdf)));

    const loadingTask = pdfjsLib.getDocument({
      data,
      isEvalSupported: false,
      disableFontFace: true,  // don't load fonts; we only need text
      cMapPacked: true,
    });
    const doc = await loadingTask.promise;

    const allRows = [];
    let currentCtx = null; // carry-forward header context across continuation pages
    let pendingRow = null; // carry a partially built row across page boundaries

    for (let i = 1; i <= doc.numPages; i++) {
      const page = await doc.getPage(i);
      const content = await page.getTextContent({ normalizeWhitespace: true });
      const pageRaw = pageTextFromItems(content, { yRound: 1, sortY: "desc" });
      const pageText = normalizeSpacesKeepNewlines(pageRaw);

      const header = extractHeaderFields(pageText);
      const hasClaimHeader = Boolean(header["Claim"]);

      // assemble rows from this page's lines, seeding with any pending buffer
      const lines = pageText.split("\n");
      const { completedRows, pending } = assembleRowsFromLines(lines, pendingRow);
      pendingRow = pending;

      // Legend-only pages: skip (keep currentCtx unchanged)
      if (isLegendOnlyPage(pageText, completedRows.length)) {
        continue;
      }

      const footerNum = pageFooterNumber(pageText);
      const hasContinuationFooter = footerNum !== null && footerNum > 1;

      // Establish / maintain context
      if (hasClaimHeader) {
        currentCtx = header; // start (or restart) context
      } else if (!currentCtx) {
        // No header & no context; avoid mis-stamping rows
        completedRows.length = 0;
      } else {
        // Continuation page: keep using currentCtx (footer or rows imply continuation)
        void hasContinuationFooter; // doc, not needed for logic now
      }

      // Emit rows only if we have context to stamp them with
      if (completedRows.length && currentCtx) {
        for (const t of completedRows) {
          const r = parseServiceRow(t);
          allRows.push({
            "Claim": currentCtx["Claim"] || "",
            "Patient": currentCtx["Patient"] || "",
            "Health Plan": currentCtx["Health Plan"] || "",
            "Participant": currentCtx["Participant"] || "",
            "Participant Id": currentCtx["Participant Id"] || "",
            "Date Entered": currentCtx["Date Entered"] || "",
            "Date Paid": currentCtx["Date Paid"] || "",
            "Provider": currentCtx["Provider"] || "",
            ...r,
          });
        }
      }

      // (Optional) react to TOTALS_RE if you want to mark end-of-claim; context persists until replaced.
      // if (TOTALS_RE.test(pageText)) { /* no-op for now */ }
    }

    // If the PDF ends with an incomplete pending row, we ignore it (not finalized).
    writeCsv(allRows, path.resolve(outputCsv));
    console.log(`Extracted ${allRows.length} service line(s) to: ${outputCsv}`);
  } catch (err) {
    console.error("Error:", err);
    process.exit(1);
  }
})();

