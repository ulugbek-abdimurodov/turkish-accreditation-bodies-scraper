# TÜRKAK Management Systems scraper

Downloads every accreditation listed under **Management Systems Certification** (type `YS`) in the
[TÜRKAK search](https://asist.turkak.org.tr/en/accreditation/accreditationagencysearch?institutionType=YS),
including suspended and withdrawn bodies.

## Run

Requires Python 3.10 or newer.

```
pip install -r requirements.txt
python turkak_scraper.py --limit 3     # trial run
python turkak_scraper.py               # everything
```

## Output (in `turkak_data/`)

| File | Contents |
| --- | --- |
| `accreditations.csv` | One row per body: status, dates, standard, contacts, certificate links, PDF paths |
| `accreditations.json` | Full API records |
| `branches.csv` | Branch sites of each body |
| `pdfs/` | Signed English certificates, e.g. `AB-0091-YS_EN.pdf` |
| `failures.json` | Failed items, only if there were any |

Bodies with no English certificate get no PDF, and the final summary counts them. Their Turkish link, if any, stays in the `CertificateTR` column of `accreditations.csv`.

## Convert for CertiFind

```
python prepare_certifind.py --download-tr --out ../../certifind/data/turkak-turkey
```

Writes `bodies.csv`, `accreditations.csv` and `scopes.csv` in the CertiFind importer format, reading
scopes from the certificate annexes. `--download-tr` first fetches the Turkish certificate
(`pdfs/AB-xxxx-YS_TR.pdf`) of every body that has no English one. Everything that needs a person's
check (normalised phones and websites, category-to-EA mappings, annexes that were not converted) is
listed in `turkak_data/REVIEW.md`.

## Options

| Option | Effect |
| --- | --- |
| `--out DIR` | Output folder |
| `--lang en\|tr` | Language of the data fields (PDFs are always the English certificate) |
| `--delay SECONDS` | Pause between requests (default 0.5) |
| `--limit N` | Process only the first N bodies |
| `--no-pdf` | Data only |
| `--force` | Download PDFs again |

Re-running resumes: saved PDFs are skipped and failures are retried. The exit code is 1 if anything failed.

## Tests

```
pip install pytest
pytest
```

The tests use a local fake of the API, so they don't touch the live site.

## API used

| Purpose | Request |
| --- | --- |
| List | `POST /Accreditation/api/v1/AccreditationAgencySearch/Search`, with `Skip` as a zero-based page index (20 per page) |
| Details | `POST /Accreditation/api/v1/AccreditationAgencySearch/DetailService/{lang}`, with the list row as the body |
| PDF | `POST /Utility/api/v1/PDF/DownloadSignedFile/{guid}/true`, with the id taken from `ShowENCert` |

This API is undocumented and may change without notice.
