#!/usr/bin/env python3
"""
Medical Claims (EOB) PDF → CSV extractor (DMBA-style, print-friendly)
Robust for:
- claims that span multiple pages (carry-forward header context),
- service rows whose descriptions wrap over multiple lines,
- rare across-page row splits (continues a partial row at top of next page),
- legend-only pages (skipped).

Deps:  Python 3.9+; pdfplumber   ->  pip install pdfplumber

Usage:
    python extract_claims_to_csv.py PrintFriendlyEOB.pdf history.csv
"""

import argparse
import csv
import re
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Tuple

import pdfplumber

# ---------------- Patterns ----------------

# Currency can be negative, with optional $ and optional parentheses.
CURRENCY = r"\(?-?\$?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d{2})?\)?"

# Final normalized service row shape (after we join wrapped lines into one):
#  01/23/2025 SOME SERVICE TEXT $123.45 $0.00 $0.00 B6 N3
SERVICE_ROW_RE = re.compile(
    rf"""^
    (?P<ServiceDate>\d{{2}}/\d{{2}}/\d{{4}})\s+   # date
    (?P<Service>.*?)\s+                            # description (lazy)
    (?P<ProviderBilled>{CURRENCY})\s+              # provider billed
    (?P<DMBA_Paid>{CURRENCY})\s+                   # DMBA paid
    (?P<YourResp>{CURRENCY})\s+                    # your responsibility
    (?P<MessageCodes>[A-Z0-9 ]{{1,40}})            # codes (e.g., '17' or 'AR' or 'B6 N3')
    $""",
    re.VERBOSE,
)

DATE_START_RE = re.compile(r"^\d{2}/\d{2}/\d{4}\b")
CURRENCY_ANY_RE = re.compile(CURRENCY)
PAGE_FOOTER_RE = re.compile(r"\bPage\s+(\d+)\b", re.I)
LEGEND_HDR_RE = re.compile(r"\bCode\s+Description\b", re.I)
LEGEND_LINE_RE = re.compile(r"^[A-Z0-9]{1,4}\b\s+.+", re.M)
TOTALS_RE = re.compile(rf"^Totals\s+{CURRENCY}\s+{CURRENCY}\s+{CURRENCY}\b", re.M)

# Header extraction (supports coloned and non-coloned labels)
HEADER_PATS = [
    ("Claim", re.compile(r"\bClaim\s*:?\s*(T\d{7,})")),
    ("Patient", re.compile(r"\bPatient\s+(.+?)\s+Health\s*Plan\b", re.S)),
    ("Health Plan", re.compile(r"\bHealth\s*Plan\s+(.+?)\s+(?:Participant|Date\s*Entered)\b", re.S)),
    ("Participant", re.compile(r"\bParticipant\s+(.+?)\s+Date\s*Entered\b", re.S)),
    ("Participant Id", re.compile(r"\bParticipant\s*Id\s+([0-9]+)\b")),
    ("Date Entered", re.compile(r"\bDate\s*Entered\s+(\d{2}/\d{2}/\d{4})\b")),
    ("Date Paid", re.compile(r"\bDate\s*Paid\s+(\d{2}/\d{2}/\d{4})\b")),
    ("Provider", re.compile(r"\bProvider\s+(.+?)(?:\n|$)")),
]

# ---------------- Utils ----------------

def _money_to_str(val: str) -> str:
    """Normalize currency string to plain 2-decimal, handling negatives and parentheses."""
    s = val.strip().replace(",", "")
    if s.startswith("$"):
        s = s[1:]
    # parentheses denote negative
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    s = s.replace("$", "")  # in case minus sits before $
    try:
        return f"{Decimal(s):.2f}"
    except (InvalidOperation, ValueError):
        return s

def normalize_page_text(raw_text: str) -> List[str]:
    """Return list of lines for a page; keep line boundaries but collapse internal runs of spaces."""
    text = (raw_text or "").replace("\xa0", " ")
    cleaned = [re.sub(r"[ \t]{2,}", " ", ln).strip() for ln in text.splitlines()]
    return cleaned

def extract_header_fields(page_text: str) -> Dict[str, Optional[str]]:
    header = {k: None for k, _ in HEADER_PATS}
    for key, pat in HEADER_PATS:
        m = pat.search(page_text)
        if m:
            header[key] = " ".join(m.group(1).split())
    return header

def page_footer_number(lines: List[str]) -> Optional[int]:
    for ln in (l for l in lines[-5:] if l):
        m = PAGE_FOOTER_RE.search(ln)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
    return None

def is_legend_only_page(page_text: str, num_service_rows: int) -> bool:
    if num_service_rows > 0:
        return False
    if LEGEND_HDR_RE.search(page_text):
        if len(LEGEND_LINE_RE.findall(page_text)) >= 2:
            return True
    return False

# ---------------- Core: join wrapped lines (incl. across pages) ----------------

def assemble_rows_from_lines(
    lines: List[str],
    pending: Optional[str] = None
) -> Tuple[List[str], Optional[str]]:
    """
    Build complete service row strings by:
    - starting a buffer at a date line,
    - appending following lines until we see three currency amounts,
    - then emit one normalized single-line string.
    Returns (completed_rows, pending_buffer_for_next_page).
    """
    rows: List[str] = []
    buf = pending  # may contain partial row carried from previous page

    def try_finalize(buffer: str) -> Optional[str]:
        # Heuristic: when there are >=3 currency tokens, it's complete.
        if buffer and len(CURRENCY_ANY_RE.findall(buffer)) >= 3:
            # Also ensure we can match the final shape:
            one_line = re.sub(r"\s+", " ", buffer).strip()
            if SERVICE_ROW_RE.match(one_line):
                return one_line
        return None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if buf is None:
            # Start new buffer only on a date line
            if DATE_START_RE.match(line):
                buf = line
                done = try_finalize(buf)
                if done:
                    rows.append(done)
                    buf = None
            else:
                # ignore non-date noise between tables / headers
                continue
        else:
            # Continue current buffer
            buf = f"{buf} {line}"
            done = try_finalize(buf)
            if done:
                rows.append(done)
                buf = None

    # return possibly incomplete buffer for next page
    return rows, buf

# ---------------- Parsers ----------------

def parse_service_row(row: str) -> Dict[str, str]:
    m = SERVICE_ROW_RE.match(row)
    assert m, f"row did not match final pattern: {row}"
    return {
        "Service Date": m.group("ServiceDate"),
        "Services Provided": m.group("Service").strip(),
        "Provider Billed ($)": _money_to_str(m.group("ProviderBilled")),
        "DMBA Paid ($)": _money_to_str(m.group("DMBA_Paid")),
        "Your Responsibility ($)": _money_to_str(m.group("YourResp")),
        "Message Codes": m.group("MessageCodes").strip(),
    }

# ---------------- Main pipeline ----------------

def parse_pdf_to_rows(pdf_path: str) -> List[Dict[str, str]]:
    all_rows: List[Dict[str, str]] = []
    current_ctx: Optional[Dict[str, Optional[str]]] = None  # carry-forward header
    pending_row: Optional[str] = None  # carry a partially built row across pages

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            # Extract and normalize text as lines
            lines = normalize_page_text(page.extract_text() or "")
            page_text = "\n".join(lines)

            # Extract header (if present)
            header = extract_header_fields(page_text)
            has_claim_header = bool(header.get("Claim"))

            # Build service-row strings from wrapped lines (and any pending chunk)
            row_texts, pending_row = assemble_rows_from_lines(lines, pending=pending_row)

            # Legend-only pages: skip, keep context
            if is_legend_only_page(page_text, len(row_texts)):
                continue

            # Continuation detection via footer (not strictly required once we’ve got rows)
            _footer = page_footer_number(lines)

            # Maintain context
            if has_claim_header:
                current_ctx = header
            else:
                # If no header and no context yet, don't emit (avoid mis-stamping)
                if current_ctx is None:
                    # If row_texts exist but we have no context, drop them safely.
                    row_texts = []

            # Emit rows stamped with current context
            if row_texts and current_ctx:
                for t in row_texts:
                    r = parse_service_row(t)
                    stamped = {
                        "Claim": current_ctx.get("Claim") or "",
                        "Patient": current_ctx.get("Patient") or "",
                        "Health Plan": current_ctx.get("Health Plan") or "",
                        "Participant": current_ctx.get("Participant") or "",
                        "Participant Id": current_ctx.get("Participant Id") or "",
                        "Date Entered": current_ctx.get("Date Entered") or "",
                        "Date Paid": current_ctx.get("Date Paid") or "",
                        "Provider": current_ctx.get("Provider") or "",
                    }
                    stamped.update(r)
                    all_rows.append(stamped)

            # (Optional) see totals; we keep context until a new header appears
            # if TOTALS_RE.search(page_text): pass

    # If the PDF ends with an incomplete buffer, we ignore it (no amounts/codes).
    return all_rows

def write_csv(rows: List[Dict[str, str]], out_path: str) -> None:
    fieldnames = [
        "Claim", "Patient", "Health Plan", "Participant", "Participant Id",
        "Date Entered", "Date Paid", "Provider",
        "Service Date", "Services Provided",
        "Provider Billed ($)", "DMBA Paid ($)", "Your Responsibility ($)",
        "Message Codes",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})

def main():
    ap = argparse.ArgumentParser(description="Extract medical claims service lines from a print-friendly EOB PDF into CSV (multi-page claims + wrapped rows).")
    ap.add_argument("pdf", help="Path to the print-friendly EOB PDF")
    ap.add_argument("csv", help="Path to output CSV file")
    args = ap.parse_args()

    rows = parse_pdf_to_rows(args.pdf)
    write_csv(rows, args.csv)
    print(f"Extracted {len(rows)} service line(s) to: {args.csv}")

if __name__ == "__main__":
    main()

