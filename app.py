import enum
import hashlib
import io
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pymupdf
import pytesseract
from flask import Flask, jsonify, request, send_file, send_from_directory
from pdfminer.high_level import extract_pages, extract_text as _pdfminer_extract_text
from pdfminer.layout import LTChar
from PIL import Image
from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, create_engine, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


class Base(DeclarativeBase):
    pass


class UUIDPrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    sessions: Mapped[list["Session"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    documents: Mapped[list["Document"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )


class Session(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Server-side session backing the auth cookie set on each user's browser."""

    __tablename__ = "sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # SHA-256 hash of the token stored in the session cookie; the raw token
    # is never persisted, so a DB leak doesn't expose valid cookies.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)

    user: Mapped["User"] = relationship(back_populates="sessions")


class UploadStatus(str, enum.Enum):
    PENDING = "pending"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "documents"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(127), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    # Key into blob storage (e.g. S3 key); the file itself never lives in the DB.
    blob_storage_key: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)

    upload_status: Mapped[UploadStatus] = mapped_column(
        Enum(UploadStatus, name="upload_status", native_enum=False),
        default=UploadStatus.PENDING,
        nullable=False,
        index=True,
    )
    upload_error: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # Overall extraction/classification confidence for the document.
    overall_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Per-field confidence scores from extraction, e.g. {"invoice_number": 0.98, "total": 0.72}.
    field_confidence_scores: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Per-field PDF source boxes, e.g. {"invoice_number": {"page": 0, "x0": .., "y0": .., "x1": .., "y1": ..}}.
    field_highlights: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Extracted shipping metadata, shown to users on the document detail view.
    bill_of_lading_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    invoice_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    shipper_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    shipper_address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    consignee_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    consignee_address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # List of {"quantity": number, "description": str, "value": number, "hts_code": str}.
    line_items: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    total_value_of_goods: Mapped[float | None] = mapped_column(Float, nullable=True)

    owner: Mapped["User"] = relationship(back_populates="documents")

    def to_display_dict(self) -> dict:
        """Display-friendly representation of a document for the detail view."""
        return {
            "id": str(self.id),
            "original_filename": self.original_filename,
            "upload_status": self.upload_status.value,
            "upload_error": self.upload_error,
            "bill_of_lading_number": self.bill_of_lading_number,
            "invoice_number": self.invoice_number,
            "shipper_name": self.shipper_name,
            "shipper_address": self.shipper_address,
            "consignee_name": self.consignee_name,
            "consignee_address": self.consignee_address,
            "line_items": self.line_items or [],
            "total_value_of_goods": self.total_value_of_goods,
            "overall_confidence": self.overall_confidence,
            "field_confidence_scores": self.field_confidence_scores or {},
            "field_highlights": self.field_highlights or {},
            "download_url": f"/documents/{self.id}/download",
            "view_url": f"/documents/{self.id}/download?view=1",
        }


DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://localhost/document_handler"
)

engine = create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


# --- Blob storage -----------------------------------------------------------
# Local-filesystem stand-in for a real object store (e.g. S3). Keys are
# namespaced by user id so two users can never collide on the same key, even
# if they upload files with identical names.

BLOB_STORAGE_ROOT = Path(os.environ.get("BLOB_STORAGE_ROOT", "./blob_storage"))


def _blob_storage_key(user: User) -> str:
    # UUID-only object name — never embed the user-supplied filename in the
    # path, or "../" segments can escape BLOB_STORAGE_ROOT.
    return f"users/{user.id}/{uuid.uuid4()}.pdf"


def _blob_storage_path(blob_storage_key: str) -> Path:
    return BLOB_STORAGE_ROOT / blob_storage_key


# --- Auth (cookie-backed sessions) ------------------------------------------

SESSION_COOKIE_NAME = "session_token"
SESSION_TTL = timedelta(days=7)


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_session(db_session, user: User) -> tuple[Session, str]:
    """Mint a new session for a user. Returns the row and the raw cookie token
    (the raw token is never persisted, only its hash is)."""
    raw_token = secrets.token_urlsafe(32)
    session_row = Session(
        user_id=user.id,
        token_hash=_hash_token(raw_token),
        expires_at=datetime.now(timezone.utc) + SESSION_TTL,
    )
    db_session.add(session_row)
    db_session.commit()
    db_session.refresh(session_row)
    return session_row, raw_token


def get_current_user(db_session) -> User | None:
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        return None

    session_row = (
        db_session.query(Session).filter_by(token_hash=_hash_token(raw_token)).one_or_none()
    )
    if session_row is None:
        return None

    now = datetime.now(timezone.utc)
    if session_row.revoked_at is not None or session_row.expires_at < now:
        return None

    session_row.last_used_at = now
    db_session.commit()
    return session_row.user


# --- Document upload/download service layer ---------------------------------


def upload_document(
    db_session, user: User, filename: str, content_type: str, file_bytes: bytes
) -> Document:
    """Store the file's bytes in blob storage and record the upload. The
    returned document's blob_storage_key is unique per user. The original
    filename is stored on the row only — never in the blob path."""
    blob_storage_key = _blob_storage_key(user)
    path = _blob_storage_path(blob_storage_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(file_bytes)

    document = Document(
        owner_id=user.id,
        original_filename=filename,
        content_type=content_type,
        size_bytes=len(file_bytes),
        blob_storage_key=blob_storage_key,
        upload_status=UploadStatus.UPLOADED,
    )
    db_session.add(document)
    db_session.commit()
    db_session.refresh(document)
    return document


# --- PDF text/field extraction -----------------------------------------------
# pdfminer.six's layout-aware extraction groups each form field's label and
# value into adjacent text blocks (unlike naive line-by-line stitching, which
# scrambles multi-column forms) — that's what makes the label-proximity
# search below workable with so little code.


def extract_text_from_pdf(path: Path) -> str:
    return _pdfminer_extract_text(str(path))


def extract_text_via_ocr(path: Path) -> str:
    """Fallback for scanned/image-only PDFs with no embedded text layer.
    Rasterizes each page (PyMuPDF) and runs Tesseract OCR on it. Returns ""
    (rather than raising) on a corrupt/unreadable file, so the caller's
    existing "no extractable text" handling covers this case too."""
    try:
        pages_text = []
        with pymupdf.open(path) as pdf:
            for page in pdf:
                pixmap = page.get_pixmap(dpi=300)
                image = Image.open(io.BytesIO(pixmap.tobytes("png")))
                pages_text.append(pytesseract.image_to_string(image))
        return "\n".join(pages_text)
    except Exception:
        return ""


_EMPTY_SHIPPING_FIELDS = {
    "bill_of_lading_number": None,
    "invoice_number": None,
    "shipper_name": None,
    "shipper_address": None,
    "consignee_name": None,
    "consignee_address": None,
    "line_items": [],
    "total_value_of_goods": None,
}

_FIELD_LABEL_PATTERNS = {
    "bill_of_lading_number": [
        # "no"/"number" suffix optional: a bare "B/L:" is a common, reliable
        # label on its own (see document1.pdf) — requiring the suffix meant
        # this never matched it, so the field fell through to a much
        # riskier fallback tier and picked up unrelated prose text instead.
        r"b\s*/\s*l[\s\-]*(?:awb)?[\s\-]*(?:no\.?|number|num)?",
        r"bill\s+of\s+lading\s*(?:no\.?|number)?",
    ],
    "invoice_number": [
        r"invoice\s*(?:no\.?|number|num|#)",
    ],
}
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-/]{2,}")
_MONEY_RE = re.compile(r"\$?\s*([\d,]+\.\d{2})")
_BLOCK_STOP_RE = re.compile(
    r"(?i)^(?:"
    r"\d+[a-z]?\.\s+"
    r"|fmc\s*:"
    r"|bill of lading\b"
    r"|commercial\s+invoice\b"
    r"|invoice\s*(?:no\.?|number|date|to|total)\b"
    r"|shipper\b"
    r"|consignee\b"
    r"|consigned to\b"
    r"|notify\b"
    r"|messrs\b"
    r"|marks\b"
    r"|page\s+\d"
    r"|terms of payment\b"
    r"|payment terms\b"
    r"|b\s*/\s*l\b"
    r"|forwarder\b"
    r"|related party\b"
    r"|place of shipment\b"
    r"|mode of transportation\b"
    r"|line\s*#"
    r"|model no\b"
    r"|description of commodities\b"
    r"|number\s+of\s+packages\b"
    r"|grand total\b"
    r"|sub\s*total\b"
    r"|shipped per\b"
    r"|from\b"
    r"|to\b"
    r"|via\b"
    r")"
)

# Same-line label matches are more reliable than neighboring-line fallthroughs.
_CONFIDENCE_SAME_LINE = 0.9
_CONFIDENCE_NEAR_LINE = 0.7
_CONFIDENCE_BLOCK = 0.75


def _non_empty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _first_digit_token(line: str) -> str | None:
    for match in _TOKEN_RE.finditer(line):
        token = match.group(0)
        if any(ch.isdigit() for ch in token):
            return token
    return None


def _parse_money(value: str) -> float | None:
    match = _MONEY_RE.search(value.replace(" ", ""))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _find_field_value_with_confidence(
    text: str, label_patterns: list[str]
) -> tuple[str | None, float | None]:
    """Return (value, confidence). Value is usually right after the label on
    the same line, or on the line directly below/above it."""
    lines = _non_empty_lines(text)

    for label_pattern in label_patterns:
        label_re = re.compile(label_pattern, re.IGNORECASE)
        for idx, line in enumerate(lines):
            match = label_re.search(line)
            if not match:
                continue

            token = _first_digit_token(line[match.end() :])
            if token:
                return token, _CONFIDENCE_SAME_LINE
            if idx + 1 < len(lines):
                token = _first_digit_token(lines[idx + 1])
                if token:
                    return token, _CONFIDENCE_NEAR_LINE
            if idx > 0:
                token = _first_digit_token(lines[idx - 1])
                if token:
                    return token, _CONFIDENCE_NEAR_LINE
    return None, None


def _find_label_line_index(lines: list[str], label_patterns: list[str]) -> int | None:
    for label_pattern in label_patterns:
        label_re = re.compile(label_pattern, re.IGNORECASE)
        for idx, line in enumerate(lines):
            if label_re.search(line):
                return idx
    return None


def _extract_block_after_label(
    lines: list[str], label_patterns: list[str], *, max_lines: int = 6
) -> list[str]:
    start = _find_label_line_index(lines, label_patterns)
    if start is None:
        return []

    block: list[str] = []
    for line in lines[start + 1 :]:
        if _BLOCK_STOP_RE.search(line) and block:
            break
        if _BLOCK_STOP_RE.search(line) and not block:
            # Skip parenthetical / helper lines under the label.
            if line.startswith("(") and line.endswith(")"):
                continue
            break
        if line.startswith("(") and line.endswith(")") and not block:
            continue
        block.append(line)
        if len(block) >= max_lines:
            break
    return block


def _split_name_address(block: list[str]) -> tuple[str | None, str | None]:
    if not block:
        return None, None
    name = block[0][:255]
    address = ", ".join(block[1:])[:500] if len(block) > 1 else None
    return name, address or None


def _extract_total_value(text: str) -> tuple[float | None, float | None]:
    # Prefer line-local parsing — `\s` matches newlines and otherwise jumps to
    # the wrong money figure (e.g. subtotal before invoice total).
    lines = _non_empty_lines(text)
    preferred_labels = (r"(?i)invoice\s*total", r"(?i)grand\s*total", r"(?i)^total\b")
    for label_re in preferred_labels:
        for idx, line in enumerate(lines):
            if not re.search(label_re, line):
                continue
            same_line = _parse_money(line)
            amounts = [same_line] if same_line and same_line > 0 else []
            for follow in lines[idx + 1 : idx + 6]:
                amount = _parse_money(follow)
                if amount is not None and amount > 0:
                    amounts.append(amount)
            if amounts:
                return amounts[-1], _CONFIDENCE_NEAR_LINE
    return None, None


def _extract_brother_line_items(text: str) -> list[dict]:
    section_match = re.search(
        r"(?is)MODEL\s*NO\.(.*?)(?:SUBTOTAL|TOTAL\s)", text
    )
    if not section_match:
        return []
    section = section_match.group(1)
    # Part numbers always contain a digit; reject header words like QUANTITY.
    parts = re.findall(
        r"(?m)^\s*((?=[A-Z0-9]*\d)[A-Z0-9]{6,})\s*\n\s*([^\n]+?)\s*$", section
    )
    extensions = [
        float(value.replace(",", ""))
        for value in re.findall(r"\$\s*([\d,]+\.\d{2})", section)
    ]
    items = []
    for idx, (part_number, description) in enumerate(parts):
        items.append(
            {
                "quantity": None,
                "description": f"{part_number} {description}".strip()[:500],
                "value": extensions[idx] if idx < len(extensions) else None,
                "hts_code": None,
            }
        )
    return items


def _extract_snapon_line_items(text: str) -> list[dict]:
    """Best-effort parse for Snap-on style commercial invoices."""
    part_numbers = re.findall(r"(?m)^\s*([A-Z]{2,}\d{3,})\s*$", text)
    # HTS codes are 8–10 digit tariff numbers; skip shorter IDs like invoice #.
    hts_codes = re.findall(r"(?m)^\s*(\d{10})\s*$", text)
    money = [
        float(value.replace(",", ""))
        for value in re.findall(r"(?m)^\s*([\d,]+\.\d{2})\s*$", text)
    ]
    qtys = [
        float(value)
        for value in re.findall(r"(?m)^\s*(\d+(?:\.\d+)?)\s*ea\s*$", text, re.I)
    ]
    # pdfminer often emits all unit prices then all extended prices for the
    # row block; ignore trailing invoice-level totals after that block.
    pair_count = len(part_numbers)
    row_money = money[: pair_count * 2]
    extended = row_money[pair_count:] if len(row_money) >= pair_count * 2 else row_money
    items = []
    for idx, part_number in enumerate(part_numbers):
        items.append(
            {
                "quantity": qtys[idx] if idx < len(qtys) else None,
                "description": part_number,
                "value": extended[idx] if idx < len(extended) else None,
                "hts_code": hts_codes[idx] if idx < len(hts_codes) else None,
            }
        )
    return items


def _extract_bol_line_items(text: str) -> list[dict]:
    section_match = re.search(
        r"(?is)DESCRIPTION OF COMMODITIES(.*?)(?:AES ITN|GROSS WEIGHT|DECLARED VALUE)",
        text,
    )
    if not section_match:
        return []
    lines = [
        line
        for line in _non_empty_lines(section_match.group(1))
        if not re.fullmatch(r"\(\d+\)", line)
    ]
    # Prefer English commodity lines when present.
    english = [line for line in lines if re.search(r"[A-Za-z]{3,}", line) and line.isupper()]
    descriptions = english or lines
    qty = None
    qty_match = re.search(r"(?i)(\d+)\s*PCS\b", text)
    if qty_match:
        qty = float(qty_match.group(1))
    # Deduplicate while preserving order.
    seen: set[str] = set()
    items = []
    for description in descriptions:
        key = description.casefold()
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "quantity": qty,
                "description": description[:500],
                "value": None,
                "hts_code": None,
            }
        )
    return items[:8]


def _set_field(
    values: dict, scores: dict, field: str, value, confidence: float | None
) -> None:
    if value is None or value == "" or value == []:
        return
    values[field] = value
    if confidence is not None:
        scores[field] = confidence


def extract_shipping_fields_with_confidence(text: str) -> tuple[dict, dict]:
    """Return full shipping metadata + per-field confidence scores."""
    values = dict(_EMPTY_SHIPPING_FIELDS)
    values["line_items"] = []
    scores: dict[str, float] = {}
    lines = _non_empty_lines(text)

    for field, patterns in _FIELD_LABEL_PATTERNS.items():
        value, confidence = _find_field_value_with_confidence(text, patterns)
        _set_field(values, scores, field, value, confidence)

    # Party blocks — label variants across BOL / commercial invoice forms.
    shipper_block = _extract_block_after_label(
        lines,
        [
            r"^\s*\d+\.?\s*exporter\b",
            r"shipper\s*\(name",
            r"^shipper\b",
        ],
    )
    if not shipper_block and re.search(r"(?i)commercial\s+invoice", text):
        # Brother-style invoices put shipper identity in the letterhead.
        header = []
        for line in lines[:6]:
            if re.search(r"(?i)commercial\s+invoice|page\s+\d|messrs", line):
                break
            header.append(line)
        shipper_block = header

    shipper_name, shipper_address = _split_name_address(shipper_block)
    _set_field(values, scores, "shipper_name", shipper_name, _CONFIDENCE_BLOCK)
    _set_field(values, scores, "shipper_address", shipper_address, _CONFIDENCE_BLOCK)

    consignee_block = _extract_block_after_label(
        lines,
        [
            r"^\s*\d+\.?\s*consigned\s+to\b",
            r"^consignee\b",
            r"^messrs\b",
        ],
    )
    consignee_name, consignee_address = _split_name_address(consignee_block)
    _set_field(values, scores, "consignee_name", consignee_name, _CONFIDENCE_BLOCK)
    _set_field(values, scores, "consignee_address", consignee_address, _CONFIDENCE_BLOCK)

    total, total_confidence = _extract_total_value(text)
    _set_field(values, scores, "total_value_of_goods", total, total_confidence)

    if re.search(r"(?i)MODEL\s*NO", text):
        line_items = _extract_brother_line_items(text)
    elif re.search(r"(?i)HTSUS|Ship\s*Qty", text):
        line_items = _extract_snapon_line_items(text)
    elif re.search(r"(?i)DESCRIPTION OF COMMODITIES", text):
        line_items = _extract_bol_line_items(text)
    else:
        line_items = []
    if line_items:
        _set_field(values, scores, "line_items", line_items, _CONFIDENCE_NEAR_LINE)

    return values, scores


def extract_shipping_fields(text: str) -> dict:
    """Best-effort extraction; returns None/[] for fields it can't find rather
    than guessing. Complex tabular layouts may still not match — this is a
    heuristic, not a full form parser."""
    values, _scores = extract_shipping_fields_with_confidence(text)
    return values


def _has_extracted_data(fields: dict) -> bool:
    for key, value in fields.items():
        if key == "line_items":
            if value:
                return True
        elif value is not None and value != "":
            return True
    return False


def _page_chars(page) -> list[LTChar]:
    chars: list[LTChar] = []

    def walk(obj):
        if isinstance(obj, LTChar):
            chars.append(obj)
        elif hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes)):
            for child in obj:
                walk(child)

    walk(page)
    return chars


def locate_text_highlight(path: Path, needle: str) -> dict | None:
    """Find the first PDF user-space bbox for needle. Coordinates are PDF
    origin (bottom-left), ready for PDF.js convertToViewportRectangle."""
    if not needle:
        return None

    for page_index, page in enumerate(extract_pages(str(path))):
        chars = _page_chars(page)
        if not chars:
            continue
        text = "".join(c.get_text() for c in chars)
        idx = text.find(needle)
        if idx < 0:
            continue
        hit = chars[idx : idx + len(needle)]
        if not hit:
            continue
        return {
            "page": page_index,
            "x0": float(min(c.x0 for c in hit)),
            "y0": float(min(c.y0 for c in hit)),
            "x1": float(max(c.x1 for c in hit)),
            "y1": float(max(c.y1 for c in hit)),
            "page_width": float(page.width),
            "page_height": float(page.height),
        }
    return None


def locate_field_highlights(path: Path, fields: dict) -> dict:
    highlights = {}
    for field, value in fields.items():
        if value is None or isinstance(value, (list, dict)):
            continue
        needle = str(value)
        if not needle:
            continue
        box = locate_text_highlight(path, needle)
        if box is not None:
            highlights[field] = box
    return highlights


_EXTRACTABLE_STATUSES = (UploadStatus.UPLOADED, UploadStatus.FAILED)


def _claim_document_for_extraction(db_session, document: Document) -> bool:
    """Atomically move UPLOADED/FAILED → PROCESSING. Returns False if another
    caller already claimed the row or it isn't eligible."""
    claimed = (
        db_session.query(Document)
        .filter(
            Document.id == document.id,
            Document.upload_status.in_(_EXTRACTABLE_STATUSES),
        )
        .update(
            {Document.upload_status: UploadStatus.PROCESSING},
            synchronize_session="fetch",
        )
    )
    db_session.commit()
    db_session.refresh(document)
    return claimed == 1


# Max length of each truncatable text field, matching the Document columns.
_FIELD_MAX_LENGTHS = {
    "bill_of_lading_number": 64,
    "invoice_number": 64,
    "shipper_name": 255,
    "shipper_address": 500,
    "consignee_name": 255,
    "consignee_address": 500,
}


def _extraction_result(
    document: Document, *, extracted_fields: dict | None = None, error: str | None = None
) -> dict:
    result = {
        "file_reference": document.blob_storage_key,
        "document": document.to_display_dict(),
        "extracted_fields": extracted_fields,
    }
    if error is not None:
        result["error"] = error
    return result


def extract_document_metadata(db_session, document: Document) -> dict:
    """Read the document's file from blob storage, extract shipping fields,
    persist them, and return {file_reference, document, extracted_fields}.

    Missing or unreadable files mark the document FAILED and surface the
    error to the caller (and via document.upload_error / to_display_dict).
    """
    if not _claim_document_for_extraction(db_session, document):
        return _extraction_result(
            document,
            error=f"document is {document.upload_status.value}; extraction skipped",
        )

    path = _blob_storage_path(document.blob_storage_key)
    try:
        if not path.is_file():
            raise FileNotFoundError("file missing from storage")
        text = extract_text_from_pdf(path)
        if not text.strip():
            # Scanned/image-only PDFs have no embedded text layer at all —
            # fall back to OCR before giving up.
            text = extract_text_via_ocr(path)
        # Layouts that don't match our labels yield text but no field
        # values; both this and a still-blank OCR result are failures —
        # raise so the shared FAILED path below handles them.
        if not text.strip():
            raise ValueError("no extractable text")
        fields, scores = extract_shipping_fields_with_confidence(text)
        if not _has_extracted_data(fields):
            raise ValueError("no shipping fields found")
        highlights = locate_field_highlights(path, fields)
    except Exception as exc:
        document.upload_status = UploadStatus.FAILED
        document.upload_error = str(exc)[:1024]
        db_session.commit()
        db_session.refresh(document)
        return _extraction_result(document, error=document.upload_error)

    for field, max_length in _FIELD_MAX_LENGTHS.items():
        value = fields.get(field) or None
        setattr(document, field, value[:max_length] if value else None)
    document.line_items = fields.get("line_items") or []
    document.total_value_of_goods = fields.get("total_value_of_goods")
    document.field_confidence_scores = scores
    document.overall_confidence = (
        sum(scores.values()) / len(scores) if scores else None
    )
    document.field_highlights = highlights
    document.upload_error = None
    document.upload_status = UploadStatus.PROCESSED
    db_session.commit()
    db_session.refresh(document)

    return _extraction_result(document, extracted_fields=fields)


def _no_store_json(payload: dict, status: int = 200):
    # Document metadata/files carry PII (names, addresses, invoice numbers) —
    # never let a browser or intermediary cache cache it.
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response, status


STATIC_DIR = Path(__file__).resolve().parent / "static"
DEMO_USER_EMAIL = "demo@localhost"

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")


def _require_owner_document(db_session, document_id: uuid.UUID):
    """Shared authz for document routes. Returns (user, document, error_response)."""
    user = get_current_user(db_session)
    if user is None:
        return None, None, _no_store_json({"error": "authentication required"}, 401)

    document = db_session.get(Document, document_id)
    if document is None:
        return user, None, _no_store_json({"error": "document not found"}, 404)
    if document.owner_id != user.id:
        return user, None, _no_store_json({"error": "forbidden"}, 403)
    return user, document, None


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.post("/auth/demo")
def demo_auth():
    """Mint a cookie-backed session for the local demo UI user."""
    with SessionLocal() as session:
        user = session.query(User).filter_by(email=DEMO_USER_EMAIL).one_or_none()
        if user is None:
            user = User(email=DEMO_USER_EMAIL, password_hash="demo")
            session.add(user)
            session.commit()
            session.refresh(user)

        _session_row, token = create_session(session, user)
        response, status = _no_store_json({"ok": True, "email": user.email})
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            httponly=True,
            samesite="Lax",
            max_age=int(SESSION_TTL.total_seconds()),
        )
        return response, status


@app.get("/documents")
def list_documents():
    with SessionLocal() as session:
        user = get_current_user(session)
        if user is None:
            return _no_store_json({"error": "authentication required"}, 401)

        documents = (
            session.query(Document)
            .filter_by(owner_id=user.id)
            .order_by(Document.created_at.desc())
            .all()
        )
        return _no_store_json({"documents": [doc.to_display_dict() for doc in documents]})


@app.post("/documents")
def create_document():
    """Upload a PDF, run extraction, return the same shape as extract_document_metadata."""
    with SessionLocal() as session:
        user = get_current_user(session)
        if user is None:
            return _no_store_json({"error": "authentication required"}, 401)

        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            return _no_store_json({"error": "file required"}, 400)

        file_bytes = uploaded.read()
        if not file_bytes:
            return _no_store_json({"error": "empty file"}, 400)

        content_type = uploaded.mimetype or "application/pdf"
        document = upload_document(
            session, user, uploaded.filename, content_type, file_bytes
        )
        result = extract_document_metadata(session, document)
        status = 201 if document.upload_status == UploadStatus.PROCESSED else 422
        return _no_store_json(result, status)


@app.get("/documents/<uuid:document_id>")
def get_document(document_id: uuid.UUID):
    with SessionLocal() as session:
        _user, document, error = _require_owner_document(session, document_id)
        if error is not None:
            return error
        return _no_store_json(document.to_display_dict())


@app.get("/documents/<uuid:document_id>/download")
def download_document(document_id: uuid.UUID):
    with SessionLocal() as session:
        _user, document, error = _require_owner_document(session, document_id)
        if error is not None:
            return error

        path = _blob_storage_path(document.blob_storage_key)
        if not path.is_file():
            return _no_store_json({"error": "file not found"}, 404)

        as_attachment = request.args.get("view") != "1"
        response = send_file(
            path,
            mimetype=document.content_type,
            as_attachment=as_attachment,
            download_name=document.original_filename,
        )
        response.headers["Cache-Control"] = "no-store"
        return response


