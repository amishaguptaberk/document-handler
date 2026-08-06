import re
from pathlib import Path

from app import UploadStatus, User, extract_document_metadata, extract_text_from_pdf, upload_document

FIXTURE_PDF = Path(__file__).parent / "fixtures" / "sample_commercial_invoice.pdf"
BOL_FIXTURE_PDF = Path(__file__).parent / "fixtures" / "sample_bill_of_lading.pdf"
SCANNED_FIXTURE_PDF = Path(__file__).parent / "fixtures" / "sample_scanned_bill_of_lading.pdf"


def test_extract_text_from_pdf_returns_non_empty_text():
    text = extract_text_from_pdf(FIXTURE_PDF)

    assert isinstance(text, str)
    assert len(text) > 0
    assert re.search(r"COMMERCIAL\s+INVOICE", text)


def test_extract_document_metadata_opens_file_and_returns_correct_metadata(
    db_session, monkeypatch, tmp_path
):
    monkeypatch.setattr("app.BLOB_STORAGE_ROOT", tmp_path)

    user = User(email="importer@example.com", password_hash="hashed-password")
    db_session.add(user)
    db_session.commit()

    document = upload_document(
        db_session,
        user,
        "invoice.pdf",
        "application/pdf",
        FIXTURE_PDF.read_bytes(),
    )

    result = extract_document_metadata(db_session, document)

    assert result["file_reference"] == document.blob_storage_key
    assert result["document"]["id"] == str(document.id)
    assert result["extracted_fields"]["bill_of_lading_number"] == "56710193"
    assert result["extracted_fields"]["invoice_number"] == "214689353"
    assert result["extracted_fields"]["shipper_name"] == "BROTHER INTERNATIONAL CORPORATION"
    assert result["extracted_fields"]["consignee_name"] == "AGENCIA JE HANDAL S A DE C.V."
    assert result["extracted_fields"]["total_value_of_goods"] == 6223.6
    assert len(result["extracted_fields"]["line_items"]) >= 1

    # persisted onto the row, not just returned
    assert document.bill_of_lading_number == "56710193"
    assert document.invoice_number == "214689353"
    assert document.shipper_name == "BROTHER INTERNATIONAL CORPORATION"
    assert document.consignee_name == "AGENCIA JE HANDAL S A DE C.V."
    assert document.total_value_of_goods == 6223.6
    assert document.line_items
    assert document.upload_status == UploadStatus.PROCESSED
    assert document.upload_error is None
    assert document.field_confidence_scores["invoice_number"] > 0
    assert document.overall_confidence > 0
    assert "invoice_number" in document.field_highlights
    assert document.field_highlights["invoice_number"]["page"] == 0
    assert result["document"]["field_highlights"]["invoice_number"]["x0"] >= 0


def test_extract_document_metadata_marks_failed_when_file_missing(
    db_session, monkeypatch, tmp_path
):
    monkeypatch.setattr("app.BLOB_STORAGE_ROOT", tmp_path)

    user = User(email="importer@example.com", password_hash="hashed-password")
    db_session.add(user)
    db_session.commit()

    document = upload_document(
        db_session,
        user,
        "invoice.pdf",
        "application/pdf",
        FIXTURE_PDF.read_bytes(),
    )
    (tmp_path / document.blob_storage_key).unlink()

    result = extract_document_metadata(db_session, document)

    assert result["error"] == "file missing from storage"
    assert result["extracted_fields"] is None
    assert result["document"]["upload_status"] == "failed"
    assert result["document"]["upload_error"] == "file missing from storage"
    assert document.upload_status == UploadStatus.FAILED
    assert document.upload_error == "file missing from storage"


def test_extract_document_metadata_marks_failed_when_no_extractable_text(
    db_session, monkeypatch, tmp_path
):
    monkeypatch.setattr("app.BLOB_STORAGE_ROOT", tmp_path)
    monkeypatch.setattr("app.extract_text_from_pdf", lambda path: "   \n")

    user = User(email="importer@example.com", password_hash="hashed-password")
    db_session.add(user)
    db_session.commit()

    document = upload_document(
        db_session, user, "scan.pdf", "application/pdf", b"%PDF-1.4"
    )

    result = extract_document_metadata(db_session, document)

    assert result["error"] == "no extractable text"
    assert document.upload_status == UploadStatus.FAILED


def test_extract_document_metadata_marks_failed_when_no_shipping_fields(
    db_session, monkeypatch, tmp_path
):
    monkeypatch.setattr("app.BLOB_STORAGE_ROOT", tmp_path)
    monkeypatch.setattr(
        "app.extract_text_from_pdf", lambda path: "Hello world, no labels here."
    )

    user = User(email="importer@example.com", password_hash="hashed-password")
    db_session.add(user)
    db_session.commit()

    document = upload_document(
        db_session, user, "other.pdf", "application/pdf", b"%PDF-1.4"
    )

    result = extract_document_metadata(db_session, document)

    assert result["error"] == "no shipping fields found"
    assert document.upload_status == UploadStatus.FAILED


def test_extract_document_metadata_parses_bl_no_label(db_session, monkeypatch, tmp_path):
    # This bill of lading only labels the field "B / L No." (bottom of the
    # form) — the header's "5a. B/L NUMBER" column has no value on the same
    # line, so the extractor must fall through to that second label.
    monkeypatch.setattr("app.BLOB_STORAGE_ROOT", tmp_path)

    user = User(email="importer@example.com", password_hash="hashed-password")
    db_session.add(user)
    db_session.commit()

    document = upload_document(
        db_session,
        user,
        "bill-of-lading.pdf",
        "application/pdf",
        BOL_FIXTURE_PDF.read_bytes(),
    )

    result = extract_document_metadata(db_session, document)

    assert result["extracted_fields"]["bill_of_lading_number"] == "HBL75421US"
    assert document.bill_of_lading_number == "HBL75421US"


def test_extract_document_metadata_falls_back_to_ocr_for_scanned_pdf(
    db_session, monkeypatch, tmp_path
):
    # This fixture has zero embedded text characters (a genuine scanned
    # image per page) — extract_text_from_pdf returns blank, so this only
    # passes if the OCR fallback (extract_text_via_ocr) actually runs and
    # Tesseract actually reads the page image correctly.
    monkeypatch.setattr("app.BLOB_STORAGE_ROOT", tmp_path)

    user = User(email="importer@example.com", password_hash="hashed-password")
    db_session.add(user)
    db_session.commit()

    document = upload_document(
        db_session,
        user,
        "scanned-bill-of-lading.pdf",
        "application/pdf",
        SCANNED_FIXTURE_PDF.read_bytes(),
    )

    result = extract_document_metadata(db_session, document)

    assert result["extracted_fields"]["bill_of_lading_number"] == "953074879"
    assert document.bill_of_lading_number == "953074879"
    assert document.upload_status == UploadStatus.PROCESSED


def test_extract_document_metadata_skips_when_not_claimable(
    db_session, monkeypatch, tmp_path
):
    monkeypatch.setattr("app.BLOB_STORAGE_ROOT", tmp_path)

    user = User(email="importer@example.com", password_hash="hashed-password")
    db_session.add(user)
    db_session.commit()

    document = upload_document(
        db_session,
        user,
        "invoice.pdf",
        "application/pdf",
        FIXTURE_PDF.read_bytes(),
    )

    first = extract_document_metadata(db_session, document)
    assert first["extracted_fields"]["invoice_number"] == "214689353"
    assert document.upload_status == UploadStatus.PROCESSED

    second = extract_document_metadata(db_session, document)
    assert second["extracted_fields"] is None
    assert "extraction skipped" in second["error"]
    assert document.upload_status == UploadStatus.PROCESSED
    assert document.invoice_number == "214689353"


def test_extract_document_metadata_allows_retry_after_failed(
    db_session, monkeypatch, tmp_path
):
    monkeypatch.setattr("app.BLOB_STORAGE_ROOT", tmp_path)

    user = User(email="importer@example.com", password_hash="hashed-password")
    db_session.add(user)
    db_session.commit()

    document = upload_document(
        db_session, user, "scan.pdf", "application/pdf", b"%PDF-1.4"
    )
    monkeypatch.setattr("app.extract_text_from_pdf", lambda path: "   \n")
    failed = extract_document_metadata(db_session, document)
    assert failed["error"] == "no extractable text"
    assert document.upload_status == UploadStatus.FAILED

    monkeypatch.setattr(
        "app.extract_text_from_pdf",
        lambda path: "Invoice number 214689353\nB/L Number HBL75421US",
    )
    monkeypatch.setattr("app.locate_field_highlights", lambda path, fields: {})

    retried = extract_document_metadata(db_session, document)
    assert retried["extracted_fields"]["invoice_number"] == "214689353"
    assert document.upload_status == UploadStatus.PROCESSED
