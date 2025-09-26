#!/usr/bin/env python3
"""
Medical Claims (EOB) PDF → CSV extractor (prints-friendly DMBA-style).

What it does
------------
- Opens a "print-friendly" Explanation of Benefits (EOB) / claims history PDF.
- Extracts per-page header fields (Claim, Patient, Health Plan, Participant, Participant Id,
  Date Entered, Date Paid, Provider) from lines where labels often appear *without colons*
  (e.g., "Patient <name> Health Plan <plan>").
- Extracts each service line that appears as a single line:
      02/05/2025 OFFICE VISIT $223.00 $0.00 $102.36 B6 N3
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

def _money_to_str(val: str) -> str:
    """Normalize currency to plain 2-decimal text (remove $ and commas)."""
    s = val.replace(",", "").strip()
    if s.startswith("$"):
        s = s[1:]
    try:
        return f"{Decimal(s):.2f}"
    except (InvalidOperation, ValueError):
        return s

# ---------- Header extraction tuned to the sample you showed ----------
# We allow either coloned or non-coloned label variants.
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

def extract_header_fields(page_text: str) -> Dict[str, Optional[str]]:
    header = {k: None for k, _ in HEADER_PATTERNS}
    for key, pat in HEADER_PATTERNS:
        m = pat.search(page_text)
        if m:
            header[key] = " ".join(m.group(1).split())
    return header

def extract_service_lines(page_text: str) -> List[Dict[str, str]]:
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

def parse_pdf_to_rows(pdf_path: str) -> List[Dict[str, str]]:
    all_rows: List[Dict[str, str]] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            # Keep newlines (row boundaries), normalize internal spacing.
            text = page.extract_text() or ""
            text = text.replace("\xa0", " ")
            cleaned_lines = [re.sub(r"[ \t]{2,}", " ", ln).strip() for ln in text.splitlines()]
            page_text = "\n".join(cleaned_lines)

            header = extract_header_fields(page_text)
            service_rows = extract_service_lines(page_text)

            for r in service_rows:
                stamped = {
                    "Claim": header.get("Claim") or "",
                    "Patient": header.get("Patient") or "",
                    "Health Plan": header.get("Health Plan") or "",
                    "Participant": header.get("Participant") or "",
                    "Participant Id": header.get("Participant Id") or "",
                    "Date Entered": header.get("Date Entered") or "",
                    "Date Paid": header.get("Date Paid") or "",
                    "Provider": header.get("Provider") or "",
                }
                stamped.update(r)
                all_rows.append(stamped)
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
    ap = argparse.ArgumentParser(description="Extract medical claims service lines from a print-friendly EOB PDF into CSV.")
    ap.add_argument("pdf", help="Path to the print-friendly EOB PDF")
    ap.add_argument("csv", help="Path to output CSV file")
    args = ap.parse_args()

    rows = parse_pdf_to_rows(args.pdf)
    write_csv(rows, args.csv)
    print(f"Extracted {len(rows)} service line(s) to: {args.csv}")

if __name__ == "__main__":
    main()

