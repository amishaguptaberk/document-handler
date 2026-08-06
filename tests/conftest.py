import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app as app_module
from app import Base

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://localhost/document_handler_test"
)


@pytest.fixture(scope="session")
def engine():
    engine = create_engine(TEST_DATABASE_URL, future=True)
    # Recreate so additive columns (e.g. field_highlights) are always present.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def db_session(engine):
    # Wrap each test in a transaction that's rolled back afterward, so tests
    # stay isolated without paying for create_all/drop_all on every test.
    connection = engine.connect()
    transaction = connection.begin()
    TestSession = sessionmaker(bind=connection, future=True)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture()
def client(db_session, monkeypatch):
    # Bind the app's session factory to the same connection as db_session,
    # so the route sees the test's uncommitted, soon-to-be-rolled-back rows.
    shared_sessionmaker = sessionmaker(bind=db_session.connection(), future=True)
    monkeypatch.setattr(app_module, "SessionLocal", shared_sessionmaker)
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as test_client:
        yield test_client
