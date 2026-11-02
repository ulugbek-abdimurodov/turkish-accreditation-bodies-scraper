#!/usr/bin/env python3
"""Turn the scraper output into a CertiFind dataset (bodies.csv, accreditations.csv, scopes.csv).

Reads turkak_data/accreditations.csv and the certificate PDFs (English, or Turkish when a
body has no English certificate), and writes:

    <out>/bodies.csv, accreditations.csv, scopes.csv   in the CertiFind importer format
    <review>                                           everything a person should check

Nothing is guessed silently: every normalised value, every category-to-EA mapping and
every annex that could not be turned into scope rows is listed in the review file.
"""
from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

logging.getLogger("pdfminer").setLevel(logging.ERROR)

AB_CODE = "TURKAK"
DEFAULT_COUNTRY = "Turkey"
# Bodies registered outside Turkey, keyed by the CityName the API reports.
FOREIGN_CITIES = {"Bakü": "Azerbaijan", "Taşkent": "Uzbekistan"}

BODY_COLUMNS = ["name", "country", "registration_no", "phone", "email", "website", "address",
                "source_url", "verified_at"]
ACCREDITATION_COLUMNS = ["ab_code", "certificate_number", "body_name", "accreditation_standard",
                         "status", "issue_date", "expiry_date", "proof_url"]
SCOPE_COLUMNS = ["certificate_number", "certification_standard", "ea_codes", "status", "scope_note"]

# Same checks as backend/.../importer/DatasetReader.java.
PHONE = re.compile(r"\+[0-9][0-9 ()/-]{5,}")
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
URL = re.compile(r"https?://\S+")

# Certification standard -> (accreditation standard, certificate suffix). TÜRKAK lists every
# scheme on one certificate (AB-xxxx-YS); CertiFind keeps one accreditation per accreditation
# standard, so the extra schemes get a suffix, as in data/dpa-albania and data/renar-romania.
SCHEMES = {
    "ISO 9001": ("ISO/IEC 17021-1", ""),
    "ISO 14001": ("ISO/IEC 17021-1", ""),
    "ISO 45001": ("ISO/IEC 17021-1", ""),
    "ISO 13485": ("ISO/IEC 17021-1", ""),
    "ISO 22301": ("ISO/IEC 17021-1", ""),
    "ISO/IEC 27001": ("ISO/IEC 27006", " (27006)"),
    "ISO/IEC 27701": ("ISO/IEC 27006", " (27006)"),
    "ISO 50001": ("ISO 50003", " (50003)"),
    "ISO 22000": ("ISO 22003-1", " (22000)"),
    "FSSC 22000": ("ISO 22003-1", " (22000)"),
    "ISO/IEC 20000-1": ("ISO/IEC 20000-6", " (20000)"),
    "ISO/IEC 42001": ("ISO/IEC 42006", " (42001)"),
}

# Recognised in annex headings; order matters (FSSC 22000 before ISO 22000, and 27701
# before 27001 because the 27701 heading reads "Extension to ISO\IEC 27001 ...").
STANDARD_PATTERNS = [
    ("FSSC 22000", r"FSSC 22000"),
    ("ISO/IEC 27701", r"ISO[\\/]IEC 27701"),
    ("ISO 9001", r"ISO 9001"),
    ("ISO 14001", r"ISO 14001"),
    ("ISO 45001", r"ISO 45001"),
    ("ISO 13485", r"ISO 13485"),
    ("ISO 22301", r"ISO 22301"),
    ("ISO/IEC 27001", r"ISO[\\/]IEC 27001"),
    ("ISO 50001", r"ISO 50001"),
    ("ISO 22000", r"ISO 22000"),
    ("ISO/IEC 20000-1", r"ISO[\\/]IEC 20000-1"),
    ("ISO/IEC 42001", r"ISO[\\/]IEC 42001"),
    ("AS 9100", r"AS ?9100"),
    ("AS 9110", r"AS ?9110"),
    ("AS 9120", r"AS ?9120"),
    ("ISO 46001", r"ISO 46001"),
]

# ---------------------------------------------------------------------------------------------
# DRAFT category -> EA mappings. TÜRKAK does not give EA codes for these schemes. The tables
# below reproduce how data/renar-romania and data/dpa-albania classified the same categories,
# so the three datasets agree. They are a judgement call: review before importing.
# ---------------------------------------------------------------------------------------------

# ISO 50003:2021 technical areas.
ENERGY_AREAS = {
    "A": "3-9;12-14;18-19;22-24",   # industry - light to medium
    "B": "10;15-17;20-21;28;34",    # industry - heavy
    "C": "30;32;36-39",             # buildings
    "D": "30;32;36-39",             # building complexes
    "E": "31",                      # transport
    "F": "2",                       # mining
    "G": "1",                       # agriculture
    "H": "25-27",                   # energy supply
}

# ISO 22003-1:2022 food chain categories (FSSC 22000 uses the same letters).
FOOD_CATEGORIES = {
    "A": "1",            # farming of animals
    "B": "1",            # farming of plants
    "C": "3",            # food, ingredient and pet food processing
    "D": "3",            # feed production
    "E": "30",           # catering / food service
    "F": "29",           # trading, retail and e-commerce
    "G": "31",           # transport and storage
    "H": "35",           # services
    "I": "7;14;15;17",   # packaging material (paper, plastic, glass, metal)
    "J": "18",           # equipment
    "K": "12;13",        # (bio)chemicals
}

# ISO 13485 main technical areas, matched on the start of the row (English or Turkish).
MEDICAL_AREAS = [
    # (name, row prefixes, EA codes)
    ("Non-active medical devices", ("non-active", "aktif olmayan"), "13;14;17"),
    ("Active implantable medical devices", ("active implantable", "aktif implant",
                                            "aktif tıbbi cihazlar (vücut içine yerleştirilebilen"), "19"),
    ("Active (non-implantable) medical devices", ("active medical devices", "aktif tıbbi cihazlar"), "19"),
    ("In vitro diagnostic medical devices", ("in vitro", "i̇n vitro", "in v tro"), "12;13;19"),
    ("Sterilization methods", ("sterilization", "tıbbi cihazlar için sterilizasyon", "steril"), "35"),
    ("Devices incorporating specific substances / technologies", ("devices incorporating", "belirli"), "13"),
    ("Parts or services", ("parts or services", "parça"), "18;29;31;34;35"),
]

ALL_NOTE = "No sector list in annex; recorded for all EA sectors"

# Table header lines, compared with spaces removed and lower-cased (the Turkish PDFs drop
# some "i" glyphs, so "Teknik" can come out as "Tekn k").
HEADERS = {
    "ea": ("eacode", "eakodu"),
    "area": ("technicalarea", "teknikalan", "teknkalan"),
    "list": ("standarddescription", "standartaçıklama", "standardaçıklama", "standartaçklama"),
    "food": ("clustercategory", "grupadı", "grupad"),
    "medical": ("maintechnicalarea", "anateknikalan", "anateknkalan"),
    "directive": ("productfamily", "ürünailesi", "ürüngrubu"),
}
BLOCK_END = re.compile(r"^(\*|This document|Bu belge|In accordance|In accordance)")
EA_ROW = re.compile(r"^(\d{1,2})([a-z])?\s+\S")
AREA_ROW = re.compile(r"^([A-H])\s+\S")
FOOD_LETTER = re.compile(r"(?<![A-Za-zÇĞİÖŞÜçğıöşü])([A-K])(?:0|IV|I{1,3})?\s?[-–]\s?\S")


@dataclass
class Block:
    standard: str | None
    kind: str
    heading: str
    page: int
    ea: set[int] = field(default_factory=set)
    letters: list[str] = field(default_factory=list)
    medical: list[str] = field(default_factory=list)
    subcodes: list[str] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)


class Review:
    def __init__(self) -> None:
        self.sections: dict[str, list[str]] = defaultdict(list)

    def add(self, section: str, line: str) -> None:
        self.sections[section].append(line)

    def write(self, path: Path, summary: list[str]) -> None:
        order = ["Draft EA mappings (check these)", "Annexes not converted", "Unparsed annex lines",
                 "Bodies without scopes", "Partial suspensions / withdrawals", "Normalised values",
                 "Foreign bodies", "Branches (not imported)"]
        out = ["# TÜRKAK dataset review", "", *summary, ""]
        for name in order + [s for s in self.sections if s not in order]:
            items = self.sections.get(name)
            if not items:
                continue
            out += [f"## {name} ({len(items)})", "", *[f"- {i}" for i in items], ""]
        path.write_text("\n".join(out), encoding="utf-8")


def header_kind(line: str) -> str | None:
    key = line.replace(" ", "").lower()
    for kind, prefixes in HEADERS.items():
        if key.startswith(prefixes):
            return kind
    return None


def standard_in(line: str) -> str | None:
    for name, pattern in STANDARD_PATTERNS:
        if re.search(pattern, line):
            return name
    return None


def annex_body_start(lines: list[str], code: str) -> int:
    """Index of the first line after the annex page header (scheme label, AB code, address)."""
    for i, line in enumerate(lines):
        if line.startswith(code) and i > 3:
            near = lines[i - 1:i + 3]
            if any("Address" in x or "Adres" in x for x in near):
                for j in range(i, min(i + 12, len(lines))):
                    if "Website" in lines[j] or "Web Sitesi" in lines[j]:
                        return j + 1
            return i + 1
    return len(lines)


def scheme_label(lines: list[str]) -> str:
    """The scheme name printed above "TS EN ISO/IEC 17021-1" on every annex page."""
    label: list[str] = []
    for line in lines[:12]:
        if line.startswith("TS EN ISO/IEC 17021-1"):
            return " ".join(label)
        if re.match(r"^(Revision Nr|Rev ?i?zyon No)", line):
            label = []
        elif "Certi" not in line and "Belgelend" not in line:
            label.append(line)
    return ""


def parse_pdf(path: Path, code: str) -> list[Block]:
    with pdfplumber.open(path) as pdf:
        pages = [(page.extract_text() or "").splitlines() for page in pdf.pages]
    blocks: list[Block] = []
    current: Block | None = None
    current_label = ""
    for number, lines in enumerate(pages[1:], start=2):   # page 1 is the certificate itself
        # The Turkish PDFs' font maps "i" to NUL.
        lines = [line.replace("\x00", "i").strip() for line in lines]
        start = annex_body_start(lines, code)
        label = scheme_label(lines)
        # A page without a table header continues the last block of the same scheme.
        open_block = current if label == current_label else None
        current_label = label
        has_header = any(header_kind(line) for line in lines[start:])
        if open_block is not None and not has_header and not starts_with_row(open_block, lines[start:]):
            open_block = None
        if open_block is None and not has_header:
            # A table format this script does not know; report it instead of guessing.
            current = None
            blocks.append(Block(standard_in(" ".join(lines[start:start + 3])), "unknown",
                                f"{label}: {' '.join(lines[start:start + 2])}", number))
            continue
        i = start
        while i < len(lines):
            line = lines[i]
            kind = header_kind(line)
            if kind:
                heading = lines[i - 1] if i > start else ""
                if kind == "directive":
                    heading = " ".join(lines[max(start, i - 3):i])
                open_block = Block(standard_in(heading), kind, heading, number)
                blocks.append(open_block)
                current = open_block
                i += 1
                continue
            if BLOCK_END.match(line):
                open_block = None
                i += 1
                continue
            if open_block is not None:
                read_row(open_block, line)
            i += 1
    return blocks


def starts_with_row(block: Block, lines: list[str]) -> bool:
    """True when a page's first content line is a row of the block's table (a continuation)."""
    first = next((line for line in lines if line), "")
    probe = Block(block.standard, block.kind, "", 0)
    read_row(probe, first)
    return bool(probe.ea or probe.letters or probe.medical) or (block.kind == "food" and first.startswith("("))


def read_row(block: Block, line: str) -> None:
    if block.kind == "ea":
        m = EA_ROW.match(line)
        if m and 1 <= int(m.group(1)) <= 39:
            block.ea.add(int(m.group(1)))
            if m.group(2):
                block.subcodes.append(m.group(1) + m.group(2))
        elif re.match(r"^\d", line):
            block.unparsed.append(line)
    elif block.kind == "area":
        m = AREA_ROW.match(line)
        if m:
            block.letters.append(m.group(1))
    elif block.kind == "food":
        for m in FOOD_LETTER.finditer(line):
            block.letters.append(m.group(1))
    elif block.kind == "medical":
        low = line.lower()
        for name, prefixes, _ in MEDICAL_AREAS:
            if low.startswith(prefixes):
                block.medical.append(name)
                break


def ea_spec(codes: set[int]) -> str:
    """{1,2,3,5} -> '1-3;5'."""
    parts, run = [], []
    for c in sorted(codes):
        if run and c == run[-1] + 1:
            run.append(c)
        else:
            if run:
                parts.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
            run = [c]
    if run:
        parts.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
    return ";".join(parts)


def expand(spec: str) -> set[int]:
    codes: set[int] = set()
    for part in spec.split(";"):
        lo, _, hi = part.partition("-")
        codes.update(range(int(lo), int(hi or lo) + 1))
    return codes


def status_of(name: str) -> str:
    if name.startswith("Active"):
        return "ACTIVE"
    if name.startswith("Suspend"):
        return "SUSPENDED"
    if name.startswith("Accreditation Withdrawn"):
        return "WITHDRAWN"
    raise ValueError(f"unknown TÜRKAK status '{name}'")


def normalise_phone(value: str) -> str:
    v = value.strip()
    if not v or PHONE.fullmatch(v):
        return v
    digits = re.sub(r"\D", "", v)
    if v.startswith("++"):
        return "+" + v.lstrip("+")
    if len(digits) == 12 and digits.startswith("90"):
        return f"+90 {digits[2:]}"
    if len(digits) == 11 and digits.startswith("0"):
        return f"+90 {digits[1:]}"
    if len(digits) == 10:
        return f"+90 {digits}"
    return v


def normalise_website(value: str) -> str:
    v = value.strip()
    if v and not re.match(r"https?://", v, re.IGNORECASE):
        v = "https://" + v
    return v


def day(value: str) -> str:
    return value.strip()[:10]


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def download_turkish(data: Path, rows: list[dict]) -> None:
    """Fetch the Turkish certificate of every body that has no English one."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import turkak_scraper as ts

    scraper = ts.Scraper(ts.make_session(), ts.API_BASE, "tr", 0.5)
    for row in rows:
        if row["PdfEN"] or not row["CertificateTR"]:
            continue
        dest = data / "pdfs" / f"{ts.safe_name(row['Code'])}_TR.pdf"
        if ts.is_pdf(dest):
            continue
        print(f"downloading {dest.name}", flush=True)
        scraper.download_pdf(ts.guid_from_url(row["CertificateTR"]), dest)
        time.sleep(0.5)


def run(args: argparse.Namespace) -> int:
    data: Path = args.data
    with open(data / "accreditations.csv", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if args.download_tr:
        download_turkish(data, rows)

    review = Review()
    bodies, accreditations, scopes = [], [], []
    with_scopes = 0

    for row in rows:
        code = row["Code"].strip()
        name = re.sub(r"\s+", " ", row["DisplayTitle"]).strip()
        status = status_of(row["StatusName"])

        # Body -------------------------------------------------------------------------------
        phone = normalise_phone(row["PhoneNumber"])
        if phone != row["PhoneNumber"].strip():
            review.add("Normalised values", f"{code} phone `{row['PhoneNumber']}` -> `{phone}`")
        website = normalise_website(row["Website"])
        if website != row["Website"].strip():
            review.add("Normalised values", f"{code} website `{row['Website']}` -> `{website}`")
        email = row["EMail"].strip()
        for column, value, pattern in (("phone", phone, PHONE), ("website", website, URL),
                                       ("email", email, EMAIL)):
            if value and not pattern.fullmatch(value):
                review.add("Normalised values", f"{code} {column} `{value}` is still invalid; left empty")
                if column == "phone":
                    phone = ""
                elif column == "website":
                    website = ""
                else:
                    email = ""
        city = row["CityName"].strip()
        country = FOREIGN_CITIES.get(city, DEFAULT_COUNTRY)
        if country != DEFAULT_COUNTRY:
            review.add("Foreign bodies", f"{code} {name}: city `{city}`, recorded as {country}")
        bodies.append({
            "name": name, "country": country, "registration_no": "", "phone": phone,
            "email": email, "website": website, "address": re.sub(r"\s+", " ", row["FullAddress"]).strip(),
            "source_url": row["HTMLPreviewLink"].strip(), "verified_at": "",
        })
        if "Some Scopes" in row["StatusName"]:
            review.add("Partial suspensions / withdrawals",
                       f"{code} {name}: TÜRKAK status `{row['StatusName']}`. The PDF annex is "
                       f"assumed to list only the scopes still held; check which ones are affected.")

        # Scopes from the certificate PDF ---------------------------------------------------
        pdf = data / row["PdfEN"] if row["PdfEN"] else data / "pdfs" / f"{code}_TR.pdf"
        blocks = parse_pdf(pdf, code) if pdf.is_file() else []
        if not pdf.is_file():
            why = "no certificate published" if not row["CertificateTR"] else "Turkish PDF not downloaded"
            review.add("Bodies without scopes",
                       f"{code} {name} [{row['StatusName']}]: {why}; body and 17021-1 accreditation only")

        by_standard: dict[str, dict] = {}
        for b in blocks:
            where = f"{code} ({pdf.name}, p.{b.page})"
            if b.kind == "unknown":
                review.add("Annexes not converted", f"{where}: unrecognised annex table `{b.heading[:110]}`")
                continue
            if b.kind == "directive":
                review.add("Annexes not converted", f"{where}: EU directive / notified-body scope "
                           f"`{b.heading[:90]}`")
                continue
            if b.standard not in SCHEMES:
                review.add("Annexes not converted", f"{where}: `{b.heading[:90]}` "
                           f"({b.standard or 'unrecognised standard'} is not in CertiFind's standards)")
                continue
            entry = by_standard.setdefault(b.standard, {"ea": set(), "notes": [], "all": False})
            for line in b.unparsed:
                review.add("Unparsed annex lines", f"{where} {b.standard}: `{line[:90]}`")
            if b.kind == "ea":
                entry["ea"] |= b.ea
                if b.subcodes:
                    entry["notes"].append("EA sub-codes " + ", ".join(sorted(set(b.subcodes))))
            elif b.kind == "list":
                entry["all"] = True
            elif b.kind == "area":
                letters = sorted(set(b.letters))
                for letter in letters:
                    entry["ea"] |= expand(ENERGY_AREAS[letter])
                entry["notes"].append(f"ISO 50003 technical areas {', '.join(letters)} (classified to EA by sector)")
                review.add("Draft EA mappings (check these)", f"{code} {b.standard}: areas {', '.join(letters)}")
            elif b.kind == "food":
                letters = sorted(set(b.letters))
                for letter in letters:
                    entry["ea"] |= expand(FOOD_CATEGORIES[letter])
                entry["notes"].append(f"ISO 22003-1 categories {', '.join(letters)} (classified to EA by sector)")
                review.add("Draft EA mappings (check these)", f"{code} {b.standard}: categories {', '.join(letters)}")
            elif b.kind == "medical":
                areas = list(dict.fromkeys(b.medical))
                for area in areas:
                    entry["ea"] |= expand(next(spec for n, _, spec in MEDICAL_AREAS if n == area))
                entry["notes"].append(f"ISO 13485 technical areas: {'; '.join(areas)} (classified to EA by sector)")
                review.add("Draft EA mappings (check these)", f"{code} {b.standard}: {'; '.join(areas)}")
            if b.kind in ("ea", "area", "food", "medical") and not (b.ea or b.letters or b.medical):
                review.add("Unparsed annex lines", f"{where} {b.standard}: table found but no rows read")

        certificates: dict[str, str] = {}   # certificate_number -> accreditation standard
        certificates[code] = "ISO/IEC 17021-1"
        for standard, entry in sorted(by_standard.items()):
            accreditation_standard, suffix = SCHEMES[standard]
            number = code + suffix
            certificates[number] = accreditation_standard
            if entry["all"] or len(entry["ea"]) == 39:
                spec = "ALL"
                if entry["all"]:
                    entry["notes"].insert(0, ALL_NOTE)
            elif entry["ea"]:
                spec = ea_spec(entry["ea"])
            else:
                continue
            scopes.append({"certificate_number": number, "certification_standard": standard,
                           "ea_codes": spec, "status": "ACTIVE",
                           "scope_note": "; ".join(dict.fromkeys(entry["notes"]))})
        if by_standard:
            with_scopes += 1
        elif pdf.is_file():
            review.add("Bodies without scopes", f"{code} {name}: PDF {pdf.name} gave no convertible scope")

        for number, accreditation_standard in certificates.items():
            accreditations.append({
                "ab_code": AB_CODE, "certificate_number": number, "body_name": name,
                "accreditation_standard": accreditation_standard, "status": status,
                "issue_date": day(row["AccreditationDate"]), "expiry_date": day(row["ValidityDate"]),
                "proof_url": row["HTMLPreviewLink"].strip(),
            })

    branches = data / "branches.csv"
    if branches.is_file():
        with open(branches, encoding="utf-8-sig", newline="") as fh:
            for b in csv.DictReader(fh):
                review.add("Branches (not imported)",
                           f"{b['Code']} {b['Title'].strip()} – {b['CityName']}, {b['DefaultPhoneNumber']}")

    args.out.mkdir(parents=True, exist_ok=True)
    scopes.sort(key=lambda s: (s["certificate_number"], s["certification_standard"]))
    write_csv(args.out / "bodies.csv", BODY_COLUMNS, bodies)
    write_csv(args.out / "accreditations.csv", ACCREDITATION_COLUMNS, accreditations)
    write_csv(args.out / "scopes.csv", SCOPE_COLUMNS, scopes)

    summary = [
        f"{len(bodies)} bodies, {len(accreditations)} accreditations, {len(scopes)} scope rows.",
        f"{with_scopes} bodies have scopes from their certificate PDF.",
        "",
        "Category-to-EA mappings are drafts copied from the RENAR/DPA datasets; see "
        "ENERGY_AREAS, FOOD_CATEGORIES and MEDICAL_AREAS in prepare_certifind.py.",
    ]
    review.write(args.review, summary)
    print("\n".join(summary[:2]))
    print(f"Wrote {args.out} and {args.review}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, default=here / "turkak_data", help="scraper output folder")
    parser.add_argument("--out", type=Path, required=True, help="dataset folder, e.g. certifind/data/turkak-turkey")
    parser.add_argument("--review", type=Path, default=None, help="review file (default: <data>/REVIEW.md)")
    parser.add_argument("--download-tr", action="store_true",
                        help="first download the Turkish certificate of bodies with no English one")
    args = parser.parse_args(argv)
    args.review = args.review or args.data / "REVIEW.md"
    return args


if __name__ == "__main__":
    sys.exit(run(parse_args()))
