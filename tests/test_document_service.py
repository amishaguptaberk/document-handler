import re
import uuid

from app import Document, UploadStatus, User, upload_document

_BLOB_KEY_RE = re.compile(
    r"^users/[0-9a-f-]{36}/[0-9a-f-]{36}\.pdf$"
)


def _make_user(db_session, email):
    user = User(email=email, password_hash="hashed-password")
    db_session.add(user)
    db_session.commit()
    return user


def test_two_users_uploading_same_filename_get_unique_blob_keys(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr("app.BLOB_STORAGE_ROOT", tmp_path)

    user_a = _make_user(db_session, "alice@example.com")
    user_b = _make_user(db_session, "bob@example.com")

    doc_a = upload_document(db_session, user_a, "report.pdf", "application/pdf", b"alice bytes")
    doc_b = upload_document(db_session, user_b, "report.pdf", "application/pdf", b"bob bytes")

    assert doc_a.blob_storage_key != doc_b.blob_storage_key
    assert doc_a.blob_storage_key.startswith(f"users/{user_a.id}/")
    assert doc_b.blob_storage_key.startswith(f"users/{user_b.id}/")
    assert _BLOB_KEY_RE.match(doc_b.blob_storage_key)
    # original name is metadata only — never part of the storage key
    assert "report.pdf" not in doc_a.blob_storage_key
    assert doc_a.original_filename == "report.pdf"

    assert (tmp_path / doc_a.blob_storage_key).read_bytes() == b"alice bytes"
    assert (tmp_path / doc_b.blob_storage_key).read_bytes() == b"bob bytes"

    assert doc_a.upload_status == UploadStatus.UPLOADED
    assert doc_b.upload_status == UploadStatus.UPLOADED
    assert doc_a.size_bytes == len(b"alice bytes")

    stored_docs = db_session.query(Document).all()
    assert len(stored_docs) == 2


def test_upload_ignores_path_traversal_in_filename(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr("app.BLOB_STORAGE_ROOT", tmp_path)

    user = _make_user(db_session, "alice@example.com")
    evil_name = "../../../../../../../../etc/passwd"

    document = upload_document(
        db_session, user, evil_name, "application/pdf", b"safe bytes"
    )

    stored_path = (tmp_path / document.blob_storage_key).resolve()
    assert stored_path.is_relative_to(tmp_path.resolve())
    assert stored_path.read_bytes() == b"safe bytes"
    assert document.original_filename == evil_name
    assert _BLOB_KEY_RE.match(document.blob_storage_key)
    assert isinstance(uuid.UUID(document.blob_storage_key.rsplit("/", 1)[-1].removesuffix(".pdf")), uuid.UUID)
