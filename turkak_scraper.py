#!/usr/bin/env python3
"""Download TURKAK accreditation records for Management Systems Certification (YS) bodies.

Writes to the output directory:
    accreditations.csv   one row per body
    accreditations.json  full API records, including branch sites
    branches.csv         one row per branch site
    pdfs/                signed English certificates, e.g. AB-0091-YS_EN.pdf
    failures.json        anything that could not be fetched (only if there were failures)

Re-running skips PDFs that are already saved, so an interrupted run can be resumed.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_BASE = "https://api.turkak.org.tr"
SITE_ORIGIN = "https://asist.turkak.org.tr"
SEARCH_PATH = "/Accreditation/api/v1/AccreditationAgencySearch/Search"
DETAIL_PATH = "/Accreditation/api/v1/AccreditationAgencySearch/DetailService/{lang}"
PDF_PATH = "/Utility/api/v1/PDF/DownloadSignedFile/{guid}/true"

INSTITUTION_TYPE = "YS"  # Management Systems Certification
CODE_SUFFIX = "-YS"
MAX_PAGES = 1000  # safety stop in case the API ever ignores paging
TIMEOUT = (15, 180)  # connect, read (seconds); certificates can be several MB

GUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)

CSV_FIELDS = [
    "Code", "DisplayTitle", "StatusName", "DecisionDate", "AccreditationDate",
    "ValidityDate", "StandardCode", "CityName", "FullAddress", "PhoneNumber",
    "EMail", "Website", "HTMLPreviewLink", "CertificateTR", "CertificateEN",
    "PdfEN", "ID",
]
BRANCH_FIELDS = ["Code", "ParentTitle", "Title", "FullAddress", "CityName", "DefaultPhoneNumber"]


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.mount("http://", HTTPAdapter(max_retries=retry))
    session.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Origin": SITE_ORIGIN,
        "Referer": SITE_ORIGIN + "/",
        "User-Agent": "turkak-scraper/1.0",
    })
    return session


class Scraper:
    def __init__(self, session: requests.Session, api_base: str, lang: str, delay: float):
        self.session = session
        self.api_base = api_base.rstrip("/")
        self.lang = lang
        self.delay = delay
        self.session.headers["LanguageCode"] = lang

    def search_all(self) -> Iterator[dict[str, Any]]:
        """Yield every YS body. `Skip` is a page index, not a record offset."""
        seen: set[str] = set()
        for page in range(MAX_PAGES):
            body = {
                "InstitutionTypes": [INSTITUTION_TYPE],
                "SubInstitution": [],
                "LangCode": self.lang,
                "Skip": page,
            }
            resp = self.session.post(self.api_base + SEARCH_PATH, json=body, timeout=TIMEOUT)
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                return
            for row in rows:
                # The server filter is re-checked so nothing else can slip in.
                if not str(row.get("Code", "")).endswith(CODE_SUFFIX) or row["ID"] in seen:
                    continue
                seen.add(row["ID"])
                yield row
            time.sleep(self.delay)
        raise RuntimeError(f"Search did not end after {MAX_PAGES} pages")

    def detail(self, row: dict[str, Any]) -> dict[str, Any]:
        resp = self.session.post(
            self.api_base + DETAIL_PATH.format(lang=self.lang),
            json={**row, "ShowDetail": True},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def download_pdf(self, guid: str, dest: Path) -> int:
        """Save a certificate atomically and return its size in bytes."""
        tmp = dest.with_name(dest.name + ".part")
        with self.session.post(
            self.api_base + PDF_PATH.format(guid=guid), stream=True, timeout=TIMEOUT
        ) as resp:
            resp.raise_for_status()
            size = 0
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    fh.write(chunk)
                    size += len(chunk)
        if not is_pdf(tmp):
            tmp.unlink(missing_ok=True)
            raise ValueError("response was not a PDF")
        os.replace(tmp, dest)
        return size


def is_pdf(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(5) == b"%PDF-"
    except OSError:
        return False


def guid_from_url(url: Any) -> str | None:
    """Certificate links look like https://asist.turkak.org.tr/en/esign/<guid>."""
    match = GUID_RE.search(url) if isinstance(url, str) else None
    return match.group(0) if match else None


def safe_name(value: str) -> str:
    """Codes come from the server, so never let one escape the output folder."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", value).strip(".") or "unknown"


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    # utf-8-sig keeps Turkish characters intact when opened in Excel.
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def run(args: argparse.Namespace) -> int:
    out: Path = args.out
    pdf_dir = out / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)

    scraper = Scraper(make_session(), args.api_base, args.lang, args.delay)
    rows = list(scraper.search_all())
    if args.limit:
        rows = rows[: args.limit]
    print(f"Found {len(rows)} Management Systems bodies", flush=True)

    records: list[dict[str, Any]] = []
    flat: list[dict[str, Any]] = []
    branches: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    saved = skipped = no_english = 0

    for index, row in enumerate(rows, 1):
        code = row.get("Code", "")
        title = row.get("DisplayTitle", "")
        print(f"[{index}/{len(rows)}] {code} {title}", flush=True)

        try:
            record = scraper.detail(row)
        except (requests.RequestException, ValueError) as exc:
            failures.append({"code": code, "step": "detail", "error": str(exc)})
            print(f"  detail failed: {exc}", flush=True)
            record = dict(row)
        detail = record.get("Detail") or {}
        time.sleep(args.delay)

        item = {**row, **detail}
        item["CertificateTR"] = detail.get("ShowTRCert") or ""
        item["CertificateEN"] = detail.get("ShowENCert") or ""
        item["PdfEN"] = ""

        url = item["CertificateEN"]
        if detail and not url:
            no_english += 1

        if url and not args.no_pdf:
            relative = f"pdfs/{safe_name(code)}_EN.pdf"
            dest = out / relative
            guid = guid_from_url(url)
            if guid is None:
                failures.append({"code": code, "step": "pdf", "error": f"no id in {url}"})
            elif not args.force and is_pdf(dest):
                item["PdfEN"] = relative
                skipped += 1
            else:
                try:
                    size = scraper.download_pdf(guid, dest)
                except (requests.RequestException, ValueError, OSError) as exc:
                    failures.append({"code": code, "step": "pdf", "error": str(exc)})
                    print(f"  PDF failed: {exc}", flush=True)
                else:
                    item["PdfEN"] = relative
                    saved += 1
                    print(f"  saved {relative} ({size / 1_048_576:.1f} MB)", flush=True)
                time.sleep(args.delay)

        records.append(record)
        flat.append(item)
        for branch in detail.get("InstituteFacilities") or []:
            branches.append({**branch, "Code": code, "ParentTitle": title})

    write_json(out / "accreditations.json", records)
    write_csv(out / "accreditations.csv", CSV_FIELDS, flat)
    write_csv(out / "branches.csv", BRANCH_FIELDS, branches)

    failure_path = out / "failures.json"
    if failures:
        write_json(failure_path, failures)
    else:
        failure_path.unlink(missing_ok=True)

    print(
        f"\nDone: {len(records)} bodies, {len(branches)} branches, "
        f"{saved} PDFs saved, {skipped} already present, "
        f"{no_english} bodies with no English certificate, {len(failures)} failures."
    )
    if failures:
        print(f"See {failure_path}; run again to retry.")
    return 1 if failures else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("turkak_data"), help="output folder (default: turkak_data)")
    parser.add_argument("--lang", choices=("en", "tr"), default="en", help="language of the data fields (default: en)")
    parser.add_argument("--delay", type=float, default=0.5, help="seconds between requests (default: 0.5)")
    parser.add_argument("--limit", type=int, default=0, help="only process the first N bodies, for a trial run")
    parser.add_argument("--no-pdf", action="store_true", help="save data only")
    parser.add_argument("--force", action="store_true", help="download PDFs again even if already saved")
    parser.add_argument("--api-base", default=API_BASE, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except requests.RequestException as exc:
        print(f"Could not reach the TURKAK API: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped. Run again to resume; saved PDFs will be skipped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
