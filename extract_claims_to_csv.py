#!/usr/bin/env python3
"""
Medical Claims (EOB) PDF → CSV extractor (DMBA-style, print-friendly).
Now robust to multi-page claims and legend-only pages.

What it does
------------
- Opens a "print-friendly" Explanation of Benefits (EOB) / claims history PDF.
- Extracts per-page header fields (Claim, Patient, Health Plan, Participant, Participant Id,
  Date Entered, Date Paid, Provider). If a claim spans multiple pages (page 2+ has no header),
  the script automatically carries forward the most recent header ("current claim context").
- Extracts each service line such as:
      02/05/2025 OFFICE VISIT $223.00 $0.00 $102.36 B6 N3
- Skips legend-only pages (e.g., pages with "Code Description" and no service rows).
- Writes one CSV containing every service line from all pages.

Dependencies
------------
- Python 3.9+
- pdfplumber  (install:  pip install pdfplumber)

Usage
-----
    python extract_claims_to_csv.py PrintFriendlyEOB.pdf history.csv
"""

import argparse
import csv
import re
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional

import pdfplumber


# ---------- Currency + service-line parsing ----------
CURRENCY = r"(?:\$?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d{2})?)"

SERVICE_ROW_RE = re.compile(
    rf"""^
    (?P<ServiceDate>\d{{2}}/\d{{2}}/\d{{4}})\s+     # 02/05/2025
    (?P<Service>.*?)\s+                             # OFFICE VISIT (lazy)
    (?P<ProviderBilled>{CURRENCY})\s+               # $223.00
    (?P<DMBA_Paid>{CURRENCY})\s+                    # $0.00
    (?P<YourResp>{CURRENCY})\s+                     # $102.36
    (?P<MessageCodes>[A-Z0-9 ]{{1,40}})             # B6 N3
    $""",
    re.VERBOSE
)

TOTALS_RE = re.compile(
    rf"""^Totals\s+{CURRENCY}\s+{CURRENCY}\s+{CURRENCY}\b""",
    re.MULTILINE
)

# ---------- Header extraction tuned to this EOB layout ----------
# Allow coloned and non-coloned label variants.
HEADER_PATTERNS = [
    ("Claim", re.compile(r"\bClaim\s*:?\s*(T\d{7,})")),
    ("Patient", re.compile(r"\bPatient\s+(.+?)\s+Health\s*Plan\b", re.S)),
    ("Health Plan", re.compile(r"\bHealth\s*Plan\s+(.+?)\s+(?:Participant|Date\s*Entered)\b", re.S)),
    ("Participant", re.compile(r"\bParticipant\s+(.+?)\s+Date\s*Entered\b", re.S)),
    ("Participant Id", re.compile(r"\bParticipant\s*Id\s+([0-9]+)\b")),
    ("Date Entered", re.compile(r"\bDate\s*Entered\s+(\d{2}/\d{2}/\d{4})\b")),
    ("Date Paid", re.compile(r"\bDate\s*Paid\s+(\d{2}/\d{2}/\d{4})\b")),
    ("Provider", re.compile(r"\bProvider\s+(.+?)(?:\n|$)")),
]

# ---------- Footer + legend detection ----------
PAGE_FOOTER_RE = re.compile(r"\bPage\s+(\d+)\b", re.I)
LEGEND_HDR_RE = re.compile(r"\bCode\s+Description\b", re.I)
LEGEND_LINE_RE = re.compile(r"^[A-Z0-9]{1,4}\b\s+.+", re.M)  # e.g., "B6 SERVICES WERE ..."


def _money_to_str(val: str) -> str:
    """Normalize a currency-ish string to plain 2-decimal text (no commas)."""
    s = val.replace(",", "").strip()
    if s.startswith("$"):
        s = s[1:]
    try:
        return f"{Decimal(s):.2f}"
    except (InvalidOperation, ValueError):
        return s  # leave as-is if not a number (rare)


def normalize_page_text(raw_text: str) -> str:
    """Preserve line breaks (row boundaries), normalize internal whitespace."""
    text = (raw_text or "").replace("\xa0", " ")
    cleaned_lines = [re.sub(r"[ \t]{2,}", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(cleaned_lines)


def extract_header_fields(page_text: str) -> Dict[str, Optional[str]]:
    """Extract header fields from a single page's text using regex heuristics."""
    header = {k: None for k, _ in HEADER_PATTERNS}
    for key, pat in HEADER_PATTERNS:
        m = pat.search(page_text)
        if m:
            header[key] = " ".join(m.group(1).split())
    return header


def extract_service_lines(page_text: str) -> List[Dict[str, str]]:
    """Find service lines on a page by scanning lines that match SERVICE_ROW_RE."""
    rows: List[Dict[str, str]] = []
    for raw in page_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = SERVICE_ROW_RE.match(line)
        if m:
            rows.append({
                "Service Date": m.group("ServiceDate"),
                "Services Provided": m.group("Service").strip(),
                "Provider Billed ($)": _money_to_str(m.group("ProviderBilled")),
                "DMBA Paid ($)": _money_to_str(m.group("DMBA_Paid")),
                "Your Responsibility ($)": _money_to_str(m.group("YourResp")),
                "Message Codes": m.group("MessageCodes").strip(),
            })
    return rows


def page_footer_number(page_text: str) -> Optional[int]:
    """Return the page number if a 'Page N' footer appears near the bottom lines."""
    lines = [ln.strip() for ln in page_text.splitlines() if ln.strip()]
    for ln in lines[-5:]:  # look at the last few lines only
        m = PAGE_FOOTER_RE.search(ln)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
    return None


def is_legend_only_page(page_text: str, num_service_rows: int) -> bool:
    """Return True if page contains a code legend and no service rows."""
    if num_service_rows > 0:
        return False
    if LEGEND_HDR_RE.search(page_text):
        # require at least a couple of legend lines so we don't skip a normal page by accident
        if len(LEGEND_LINE_RE.findall(page_text)) >= 2:
            return True
    return False


def parse_pdf_to_rows(pdf_path: str) -> List[Dict[str, str]]:
    all_rows: List[Dict[str, str]] = []
    current_ctx: Optional[Dict[str, Optional[str]]] = None  # carry-forward header context

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = normalize_page_text(page.extract_text() or "")
            header = extract_header_fields(text)
            has_claim_header = bool(header.get("Claim"))

            # Extract service rows first (used in continuation detection and legend skipping)
            service_rows = extract_service_lines(text)
            num_rows = len(service_rows)

            # Legend-only pages: skip, keep context untouched
            if is_legend_only_page(text, num_rows):
                continue

            # Footer hint (Page N)
            footer_num = page_footer_number(text)
            has_continuation_footer = footer_num is not None and footer_num > 1

            # Establish/maintain context
            if has_claim_header:
                # Start (or restart) context with this page's header
                current_ctx = header
            else:
                # No header: treat as continuation if we have context and either:
                #  - there are service rows, or
                #  - the footer says Page N (N>1)
                if current_ctx is None:
                    # No context to carry forward; if page has rows we'd rather not mis-stamp them.
                    # Safely skip emitting rows until a real header appears.
                    if num_rows > 0:
                        # (Optional) You could log a warning here.
                        pass
                else:
                    # valid continuation context already set
                    pass

            # Emit rows (only when we have a context to stamp)
            if num_rows > 0 and current_ctx:
                for r in service_rows:
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

            # (Optional) detect end-of-claim via Totals line; we don't need to clear context here.
            # If you later want to sanity-check boundaries, you can record that totals were seen:
            # if TOTALS_RE.search(text): seen_totals_for_current_ctx = True

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
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in fieldnames})


def main():
    ap = argparse.ArgumentParser(description="Extract medical claims service lines from a print-friendly EOB PDF into CSV (multi-page claims supported).")
    ap.add_argument("pdf", help="Path to the print-friendly EOB PDF")
    ap.add_argument("csv", help="Path to output CSV file")
    args = ap.parse_args()

    rows = parse_pdf_to_rows(args.pdf)
    write_csv(rows, args.csv)
    print(f"Extracted {len(rows)} service line(s) to: {args.csv}")


if __name__ == "__main__":
    main()

