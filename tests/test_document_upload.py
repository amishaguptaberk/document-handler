import uuid

from app import Document, UploadStatus, User


def _make_user(db_session, email):
    user = User(email=email, password_hash="hashed-password")
    db_session.add(user)
    db_session.commit()
    return user


def _upload_document(db_session, user, filename, blob_key):
    document = Document(
        owner_id=user.id,
        original_filename=filename,
        content_type="application/pdf",
        size_bytes=1024,
        blob_storage_key=blob_key,
        upload_status=UploadStatus.UPLOADED,
    )
    db_session.add(document)
    db_session.commit()
    return document


def test_two_users_get_unique_ids(db_session):
    user_a = _make_user(db_session, "alice@example.com")
    user_b = _make_user(db_session, "bob@example.com")

    assert user_a.id != user_b.id
    assert isinstance(user_a.id, uuid.UUID)
    assert isinstance(user_b.id, uuid.UUID)


def test_two_users_can_each_upload_a_document(db_session):
    user_a = _make_user(db_session, "alice@example.com")
    user_b = _make_user(db_session, "bob@example.com")

    doc_a = _upload_document(db_session, user_a, "alice-invoice.pdf", "blobs/alice-invoice.pdf")
    doc_b = _upload_document(db_session, user_b, "bob-invoice.pdf", "blobs/bob-invoice.pdf")

    stored_docs = db_session.query(Document).all()
    assert len(stored_docs) == 2

    assert doc_a.id != doc_b.id
    assert doc_a.owner_id == user_a.id
    assert doc_b.owner_id == user_b.id
    assert doc_a.upload_status == UploadStatus.UPLOADED
    assert doc_b.upload_status == UploadStatus.UPLOADED

    assert db_session.query(Document).filter_by(owner_id=user_a.id).one().id == doc_a.id
    assert db_session.query(Document).filter_by(owner_id=user_b.id).one().id == doc_b.id


def test_two_users_uploading_same_filename_get_distinct_blob_keys(db_session):
    user_a = _make_user(db_session, "alice@example.com")
    user_b = _make_user(db_session, "carol@example.com")

    doc_a = _upload_document(db_session, user_a, "report.pdf", "blobs/alice/report.pdf")
    doc_b = _upload_document(db_session, user_b, "report.pdf", "blobs/carol/report.pdf")

    assert doc_a.blob_storage_key != doc_b.blob_storage_key
    assert {doc_a.owner_id, doc_b.owner_id} == {user_a.id, user_b.id}
