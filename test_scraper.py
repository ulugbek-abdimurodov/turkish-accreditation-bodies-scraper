"""Offline tests: run the whole scraper against a local fake of the TURKAK API."""
import csv
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import turkak_scraper as ts

GUID = "496a4c3c-0882-49bf-ae63-3ab152c9f41{}"
PDF = b"%PDF-1.7 fake certificate"


def row(n, suffix="-YS"):
    return {"ID": f"id-{n}", "Code": f"AB-{n:04d}{suffix}", "DisplayTitle": f"Şirket {n}", "StatusName": "Active",
            "DecisionDate": "2026-10-28 10:11", "OnlySecure": False, "ShowDetail": False}


class FakeApi(BaseHTTPRequestHandler):
    pages = []
    pdf_calls = []
    search_bodies = []
    bad_pdf = set()

    def log_message(self, *args):
        pass

    def send(self, payload, ctype="application/json", status=200):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # the real PDF endpoint returns 404 for GET
        self.send(b"", status=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/Search"):
            type(self).search_bodies.append(body)
            pages = type(self).pages
            self.send(pages[body["Skip"]] if body["Skip"] < len(pages) else [])
        elif "/DetailService/" in self.path:
            n = int(body["ID"].split("-")[1])
            detail = {"StandardCode": "TS EN ISO/IEC 17021-1:2015", "CityName": "Ankara",
                      "InstituteFacilities": [{"Title": f"Şube {n}", "CityName": "Varna"}]}
            if n == 1:  # both languages
                detail["ShowTRCert"] = f"https://asist.turkak.org.tr/en/esign/{GUID.format(9)}"
            if n == 3:  # Turkish only, like some live records
                detail["ShowTRCert"] = f"https://asist.turkak.org.tr/en/esign/{GUID.format(8)}"
            else:
                detail["ShowENCert"] = f"https://asist.turkak.org.tr/en/esign/{GUID.format(n)}"
            self.send({**body, "Detail": detail})
        elif "/DownloadSignedFile/" in self.path:
            guid = self.path.split("/")[-2]
            type(self).pdf_calls.append(guid)
            if guid in type(self).bad_pdf:
                self.send(b"<html>error</html>", "text/html")
            else:
                self.send(PDF, "application/pdf")
        else:
            self.send(b"", status=404)


@pytest.fixture()
def api():
    FakeApi.pages = [[row(1), row(2)], [row(3), row(4, suffix="-LAB"), row(1)]]
    FakeApi.pdf_calls = []
    FakeApi.search_bodies = []
    FakeApi.bad_pdf = set()
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def run(api, out, *extra):
    return ts.main(["--api-base", api, "--out", str(out), "--delay", "0", *extra])


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def test_full_run(api, tmp_path):
    assert run(api, tmp_path) == 0
    assert FakeApi.search_bodies[0]["InstitutionTypes"] == ["YS"]
    assert [b["Skip"] for b in FakeApi.search_bodies] == [0, 1, 2]

    rows = read_csv(tmp_path / "accreditations.csv")
    assert [r["Code"] for r in rows] == ["AB-0001-YS", "AB-0002-YS", "AB-0003-YS"]  # no -LAB, no duplicate
    assert rows[0]["DisplayTitle"] == "Şirket 1"
    assert rows[0]["PdfEN"] == "pdfs/AB-0001-YS_EN.pdf"
    assert rows[2]["PdfEN"] == ""
    assert rows[2]["CertificateTR"] != ""  # the Turkish link is kept, the file is not downloaded
    assert sorted(FakeApi.pdf_calls) == [GUID.format(1), GUID.format(2)]

    assert sorted(p.name for p in (tmp_path / "pdfs").iterdir()) == [
        "AB-0001-YS_EN.pdf", "AB-0002-YS_EN.pdf"]
    assert len(read_csv(tmp_path / "branches.csv")) == 3
    assert len(json.loads((tmp_path / "accreditations.json").read_text(encoding="utf-8"))) == 3
    assert not (tmp_path / "failures.json").exists()


def test_resume_skips_saved_pdfs(api, tmp_path):
    run(api, tmp_path)
    FakeApi.pdf_calls.clear()
    assert run(api, tmp_path) == 0
    assert FakeApi.pdf_calls == []
    assert read_csv(tmp_path / "accreditations.csv")[0]["PdfEN"] == "pdfs/AB-0001-YS_EN.pdf"


def test_non_pdf_is_reported_and_retried(api, tmp_path):
    FakeApi.bad_pdf = {GUID.format(2)}
    assert run(api, tmp_path) == 1
    assert not (tmp_path / "pdfs" / "AB-0002-YS_EN.pdf").exists()
    assert not list((tmp_path / "pdfs").glob("*.part"))
    assert json.loads((tmp_path / "failures.json").read_text(encoding="utf-8"))[0]["code"] == "AB-0002-YS"

    FakeApi.bad_pdf = set()
    FakeApi.pdf_calls.clear()
    assert run(api, tmp_path) == 0
    assert FakeApi.pdf_calls == [GUID.format(2)]
    assert not (tmp_path / "failures.json").exists()


def test_no_pdf_and_limit(api, tmp_path):
    assert run(api, tmp_path, "--no-pdf", "--limit", "1") == 0
    assert FakeApi.pdf_calls == []
    assert len(read_csv(tmp_path / "accreditations.csv")) == 1


def test_safe_name_blocks_path_escape():
    assert "/" not in ts.safe_name("../../etc/passwd")
    assert not ts.safe_name("../x").startswith(".")
