import uuid
from pathlib import Path

from app import SESSION_COOKIE_NAME, Document, UploadStatus, User, create_session, upload_document


def _authenticate(client, db_session, user):
    _, token = create_session(db_session, user)
    client.set_cookie(SESSION_COOKIE_NAME, token)


def test_get_document_returns_display_friendly_metadata(client, db_session):
    user = User(email="importer@example.com", password_hash="hashed-password")
    db_session.add(user)
    db_session.commit()

    document = Document(
        owner_id=user.id,
        original_filename="shipment-42.pdf",
        content_type="application/pdf",
        size_bytes=2048,
        blob_storage_key="blobs/shipment-42.pdf",
        upload_status=UploadStatus.PROCESSED,
        bill_of_lading_number="BOL-12345",
        invoice_number="INV-98765",
        shipper_name="Acme Exports Ltd",
        shipper_address="1 Harbor Way, Long Beach, CA",
        consignee_name="Globex Imports Inc",
        consignee_address="9 Market St, Newark, NJ",
        line_items=[
            {
                "quantity": 100,
                "description": "Steel bolts",
                "value": 500.0,
                "hts_code": "7318.15.20",
            },
            {
                "quantity": 50,
                "description": "Aluminum brackets",
                "value": 250.0,
                "hts_code": "7616.99.51",
            },
        ],
        total_value_of_goods=750.0,
    )
    db_session.add(document)
    db_session.commit()

    _authenticate(client, db_session, user)
    response = client.get(f"/documents/{document.id}")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.get_json()

    # display-friendly: JSON-serializable id/enum, not raw UUID/Enum objects
    assert body["id"] == str(document.id)
    assert isinstance(body["id"], str)
    assert body["upload_status"] == "processed"

    assert body["bill_of_lading_number"] == "BOL-12345"
    assert body["invoice_number"] == "INV-98765"
    assert body["shipper_name"] == "Acme Exports Ltd"
    assert body["shipper_address"] == "1 Harbor Way, Long Beach, CA"
    assert body["consignee_name"] == "Globex Imports Inc"
    assert body["consignee_address"] == "9 Market St, Newark, NJ"
    assert body["total_value_of_goods"] == 750.0

    assert body["line_items"] == [
        {
            "quantity": 100,
            "description": "Steel bolts",
            "value": 500.0,
            "hts_code": "7318.15.20",
        },
        {
            "quantity": 50,
            "description": "Aluminum brackets",
            "value": 250.0,
            "hts_code": "7616.99.51",
        },
    ]


def test_get_document_returns_404_when_missing(client, db_session):
    user = User(email="importer@example.com", password_hash="hashed-password")
    db_session.add(user)
    db_session.commit()
    _authenticate(client, db_session, user)

    response = client.get(f"/documents/{uuid.uuid4()}")
    assert response.status_code == 404


def test_get_document_requires_authentication(client, db_session):
    user = User(email="importer@example.com", password_hash="hashed-password")
    db_session.add(user)
    db_session.commit()
    document = Document(
        owner_id=user.id,
        original_filename="shipment-42.pdf",
        content_type="application/pdf",
        size_bytes=2048,
        blob_storage_key="blobs/shipment-42.pdf",
        upload_status=UploadStatus.PROCESSED,
    )
    db_session.add(document)
    db_session.commit()

    response = client.get(f"/documents/{document.id}")
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"


def test_get_document_forbidden_for_non_owner(client, db_session):
    owner = User(email="owner@example.com", password_hash="hashed-password")
    other = User(email="other@example.com", password_hash="hashed-password")
    db_session.add_all([owner, other])
    db_session.commit()
    document = Document(
        owner_id=owner.id,
        original_filename="shipment-42.pdf",
        content_type="application/pdf",
        size_bytes=2048,
        blob_storage_key="blobs/shipment-42.pdf",
        upload_status=UploadStatus.PROCESSED,
    )
    db_session.add(document)
    db_session.commit()

    _authenticate(client, db_session, other)
    response = client.get(f"/documents/{document.id}")
    assert response.status_code == 403


def test_download_requires_owner_cookie(client, db_session, monkeypatch, tmp_path):
    monkeypatch.setattr("app.BLOB_STORAGE_ROOT", tmp_path)

    owner = User(email="owner@example.com", password_hash="hashed-password")
    other = User(email="other@example.com", password_hash="hashed-password")
    db_session.add_all([owner, other])
    db_session.commit()

    document = upload_document(
        db_session, owner, "manifest.pdf", "application/pdf", b"manifest bytes"
    )

    # no cookie -> unauthenticated
    response = client.get(f"/documents/{document.id}/download")
    assert response.status_code == 401

    # valid cookie, but not the owner -> forbidden
    _authenticate(client, db_session, other)
    response = client.get(f"/documents/{document.id}/download")
    assert response.status_code == 403

    # valid cookie for the owner -> the file's bytes
    _authenticate(client, db_session, owner)
    response = client.get(f"/documents/{document.id}/download")
    assert response.status_code == 200
    assert response.data == b"manifest bytes"
    assert response.headers["Content-Type"].startswith("application/pdf")


def test_download_returns_404_when_blob_missing(client, db_session, monkeypatch, tmp_path):
    monkeypatch.setattr("app.BLOB_STORAGE_ROOT", tmp_path)

    owner = User(email="owner@example.com", password_hash="hashed-password")
    db_session.add(owner)
    db_session.commit()

    document = upload_document(
        db_session, owner, "manifest.pdf", "application/pdf", b"manifest bytes"
    )
    (tmp_path / document.blob_storage_key).unlink()

    _authenticate(client, db_session, owner)
    response = client.get(f"/documents/{document.id}/download")

    assert response.status_code == 404
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_json() == {"error": "file not found"}


def test_demo_auth_upload_and_list_documents(client, db_session, monkeypatch, tmp_path):
    monkeypatch.setattr("app.BLOB_STORAGE_ROOT", tmp_path)
    fixture = Path(__file__).parent / "fixtures" / "sample_commercial_invoice.pdf"

    auth = client.post("/auth/demo")
    assert auth.status_code == 200
    assert SESSION_COOKIE_NAME in response_cookie_names(auth)

    upload = client.post(
        "/documents",
        data={"file": (fixture.open("rb"), "invoice.pdf")},
        content_type="multipart/form-data",
    )
    assert upload.status_code == 201
    body = upload.get_json()
    assert body["document"]["upload_status"] == "processed"
    assert body["document"]["invoice_number"] == "214689353"
    assert body["document"]["field_confidence_scores"]["invoice_number"] > 0
    assert "invoice_number" in body["document"]["field_highlights"]
    assert body["document"]["view_url"].endswith("?view=1")

    listed = client.get("/documents")
    assert listed.status_code == 200
    docs = listed.get_json()["documents"]
    assert len(docs) == 1
    assert docs[0]["id"] == body["document"]["id"]

    inline = client.get(body["document"]["view_url"])
    assert inline.status_code == 200
    assert "attachment" not in inline.headers.get("Content-Disposition", "").lower()


def response_cookie_names(response):
    # Flask test client may expose set-cookie via headers differently across versions.
    cookie_header = response.headers.getlist("Set-Cookie")
    return {part.split("=", 1)[0] for header in cookie_header for part in [header.split(";", 1)[0]]}
