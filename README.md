# document-handler

Upload a shipping PDF (bill of lading, commercial invoice) and it pulls out
the shipping data: bill of lading number, invoice number, shipper and
consignee name/address, line items, total value of goods. Each field shows
up next to a highlighted view of the source page so you can check it
against the actual document instead of trusting a JSON blob.

Built in order: data model and routes, upload/download, PII protections,
extraction, OCR for scanned docs, line items and confidence scoring and
highlighting, then a cleanup pass. Every extraction rule was tested against
real sample PDFs, not made-up text, which gives real coverage on the four
documents used to build it. Extending that to new formats is mostly the
work described in "If I had another hour" below.

## Endpoints

- `POST /documents`: upload a PDF, extract fields, return the result
- `GET /documents`: list the current user's documents
- `GET /documents/<id>`: one document's metadata (owner only)
- `GET /documents/<id>/download`: download the file (owner only). `?view=1`
  serves it inline for the preview panel instead of as an attachment
- `POST /auth/demo`: the only way to get a session right now (see Auth)

## Verification note

Most of this is tested against real behavior, not just reasoned about:
owner checks, path traversal prevention, and the OCR fallback all have
tests that exercise the real code path with real inputs (a real scanned
PDF through real Tesseract, a real traversal-shaped filename, and so on).

One spot where that's not true yet: the extraction lock
(`_claim_document_for_extraction`) uses an atomic conditional
`UPDATE ... WHERE status IN (...)` to stop two callers from processing the
same document at once, which is the right approach, but the current test
only calls it twice in a row on one thread. That proves the state machine,
not the race. Closing it properly means two threads, two separate DB
sessions on the same engine, both loading the same row, released together
via `threading.Barrier(2)`, asserting exactly one gets real extraction and
the other gets skipped. Listed in "If I had another hour" below.

## How extraction works

Everything lives in one `app.py` on purpose. Small enough that splitting
it up would just add overhead right now.

**pdfminer.six, not pdfplumber.** Started with pdfplumber, but its
`extract_text()` stitches lines by raw position and scrambles multi-column
forms. A label and its value would end up on unrelated lines with
unrelated text between them. Switched to pdfminer.six's `extract_text()`
directly, whose layout-aware grouping keeps each label and value together
as a block. That's what makes a simple proximity search work at all.

**Field matching.** Single-token fields (bill of lading number, invoice
number) look for a label, then check the same line, then the next line,
then the previous line, and take the first token with a digit in it.
Name/address fields (shipper, consignee) split the text into blocks on
blank lines and take the block after the label as name plus address. Total
value scans a window after a total-related label and takes the last
currency-looking number in it, since totals tend to come last in a stack
of subtotal/fees/total. Line items are the exception: three hand-written
parsers keyed on vendor-specific keywords, not a general table parser.
That's a deliberate choice to avoid building an abstraction for three
fixed cases, but it means a new vendor format needs a new parser.

Every matcher returns `None`/`[]` instead of guessing when it can't find
something, and that's tested.

**OCR fallback.** Some PDFs are pure scans with zero embedded text
characters, confirmed directly by checking `page.chars == 0`. No text
extractor can pull anything from that since there's no text there, only
pixels. When the primary extraction comes back blank, the app rasterizes
each page with PyMuPDF and runs Tesseract OCR on the images. This only
runs as a fallback since OCR is slower and less accurate than reading real
text.

**Highlighting.** After a field's value is found, the app re-parses the
PDF's character positions to find where that text sits on the page, then
returns a bounding box. The frontend uses pdf.js to render the page and
draws a highlight over that box. Clicking a field scrolls to and
highlights it.

**Failure handling.** A missing file, no extractable text, or no matched
fields are all marked `FAILED` with a specific error message, not a silent
empty result. A document can be retried after failing. The claim/lock step
means two extraction attempts on the same document can't both proceed, in
theory (see the test gap above).

**Uploads and storage.** Files go to `users/{user_id}/{uuid}.pdf` on local
disk, standing in for something like S3. The original filename is stored
only as metadata, never used in the path, since a filename like
`../../../etc/passwd` would be a path traversal bug. Namespacing by user
also means two users can't collide on a filename.

## Auth

This was a stretch goal, not the focus. What's there is real: hashed
session tokens, expiry, revocation, and a correct owner check on every
document route. What's missing is real login. `/auth/demo` gives every
visitor the same shared user, so the owner check has nothing to actually
separate. If this becomes a real feature: add real signup/login, set
`secure=True` on the cookie once served over HTTPS, and fill in the unused
audit columns.

## If I had another hour

1. Write the concurrency test for the extraction lock. Detailed above: two
   threads, two DB sessions, one row, a barrier to line them up, assert
   exactly one caller gets real extraction and the other gets skipped.
2. Already done: OCR regression test against a real scanned PDF.
3. Replace the vendor-specific line-item parsers with column clustering
   based on character x-position, so a new document format doesn't need
   new code.
4. Try pdfplumber's table detection for line items specifically, since
   that's a different, genuinely useful capability from its plain text
   extraction.
5. Use cross-page consistency as a confidence signal (a value that repeats
   identically across pages is probably correct).
6. Sanity check extracted totals against the sum of line items and flag
   mismatches instead of trusting whichever regex matched.
7. Add an LLM fallback for documents that match no known pattern, only
   used when the fast path finds nothing.
8. Parse the PDF once and reuse it for both extraction and highlighting
   instead of re-parsing per field.

## Setup (first time)

Needs Python 3.11+, local Postgres, and Tesseract (for OCR):

```bash
brew install postgresql tesseract   # or your OS's equivalent
createdb document_handler
createdb document_handler_test

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running it

```bash
source .venv/bin/activate
python -c "import app; app.app.run(port=5000)"
```

Opens at `http://localhost:5000`. Uses the `document_handler` database and
local disk storage under `./blob_storage/` by default; both are
configurable via the `DATABASE_URL` and `BLOB_STORAGE_ROOT` env vars.

## Testing

Runs against `document_handler_test`, a real Postgres database, not
SQLite, since the `JSONB`/`UUID` columns don't work on SQLite anyway and
testing against the same engine as production avoids a whole class of
bugs. Each test runs in a transaction that gets rolled back. Extraction
tests use real sample PDFs in `tests/fixtures/`, so a regex change that
breaks real-world extraction fails immediately.

```bash
source .venv/bin/activate
pytest tests/ -v
```
