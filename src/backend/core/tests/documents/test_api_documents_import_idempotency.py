"""
Tests for idempotent DOCX/Markdown imports on the documents endpoints.

The replayable identity is the client-provided document ID: retrying the same
identity with the same file and target parent returns the original document,
concurrent requests run the conversion once, an identity reused with a
different file or target parent is rejected, and failed/interrupted imports
can be safely retried.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch
from uuid import uuid4

from django.db import connection

import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from rest_framework.test import APIClient

from core import factories, models
from core.services import mime_types
from core.services.converter_services import ConversionError
from core.services.document_import_service import (
    _store_imported_content,
    compute_file_fingerprint,
)
from core.utils.analytics import PosthogEventName

pytestmark = pytest.mark.django_db


class _FakeS3Client:
    """Minimal in-memory S3 client (head/get object) for hermetic tests."""

    def __init__(self, objects):
        self.objects = objects

    @staticmethod
    def _not_found(operation):
        return ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, operation
        )

    def head_object(self, **kwargs):
        """Mimic boto3 head_object, returning a 404 error for unknown keys."""
        key = kwargs["Key"]
        if key not in self.objects:
            raise self._not_found("HeadObject")
        return {"ETag": '"unused"'}

    def get_object(self, **kwargs):
        """Mimic boto3 get_object with a streaming body."""
        key = kwargs["Key"]
        if key not in self.objects:
            raise self._not_found("GetObject")
        data = self.objects[key]
        return {"Body": StreamingBody(BytesIO(data), len(data))}


class _FakeStorage:
    """In-memory storage exposing the S3 boto3 connection used by models."""

    bucket_name = "test-bucket"

    def __init__(self):
        self.objects = {}
        self.connection = SimpleNamespace(
            meta=SimpleNamespace(client=_FakeS3Client(self.objects))
        )

    def save(self, name, content, max_length=None):
        """Store the file content in memory (Django storage API)."""
        _ = max_length
        self.objects[name] = content.read()
        return name


@pytest.fixture(autouse=True)
def s3_storage():
    """Replace S3-backed storage for the import tests (shared by model/service)."""
    storage = _FakeStorage()
    with (
        mock.patch("core.models.default_storage", storage),
        mock.patch(
            "core.services.document_import_service.default_storage", storage
        ),
    ):
        yield storage
    storage.objects.clear()

FILENAME = "My Important Document.docx"
FILE_CONTENT = b"fake docx content"
CONVERTED_YJS = "base64encodedyjscontent"


def make_file(content=FILE_CONTENT, name=FILENAME):
    """Build a fake uploaded DOCX file."""
    file_obj = BytesIO(content)
    file_obj.name = name
    return file_obj


def fingerprint(
    file_content=FILE_CONTENT,
    filename=FILENAME,
    content_type=mime_types.DOCX,
):
    """Compute the import fingerprint expected on the DocumentImport record."""
    return compute_file_fingerprint(filename, content_type, file_content)


def owner_access_count(user):
    """Count owner accesses of a user."""
    return models.DocumentAccess.objects.filter(user=user, role="owner").count()


def post_import(client, import_id, *, file_obj=None, parent_id=None, **payload):
    """Post a file import (root or child) with the given identity."""
    payload.setdefault("file", file_obj if file_obj is not None else make_file())
    if import_id is not None:
        payload["id"] = str(import_id)
    url = (
        f"/api/v1.0/documents/{parent_id!s}/children/"
        if parent_id is not None
        else "/api/v1.0/documents/"
    )
    return client.post(url, payload, format="multipart")


@pytest.fixture(name="import_user")
def import_user_fixture():
    """Create a logged-in API client with its user."""
    user = factories.UserFactory()
    client = APIClient()
    client.force_login(user)
    return user, client


@patch("core.services.document_import_service.Converter.convert")
def test_import_root_replays_same_identity(mock_convert, settings, import_user):
    """Retrying the same identity with the same file returns the original document."""
    settings.CONVERSION_UPLOAD_ENABLED = True
    mock_convert.return_value = CONVERTED_YJS
    user, client = import_user
    import_id = uuid4()

    with patch("core.api.viewsets.posthog_capture") as mock_capture:
        response1 = post_import(client, import_id)
        response2 = post_import(client, import_id)

    assert response1.status_code == 201
    assert response2.status_code == 200

    assert response1.json()["id"] == str(import_id)
    assert response2.json()["id"] == str(import_id)

    documents = models.Document.objects.all()
    assert len(documents) == 1
    document = documents[0]
    assert document.id == import_id
    assert document.title == FILENAME
    assert document.content == CONVERTED_YJS
    assert document.accesses.filter(role="owner", user=user).count() == 1

    # The expensive conversion runs only once.
    mock_convert.assert_called_once_with(
        FILE_CONTENT, content_type=mime_types.DOCX, accept=mime_types.YJS
    )

    # Analytics are emitted once, on the actual creation, never on replay.
    assert mock_capture.call_count == 2
    mock_capture.assert_any_call(
        PosthogEventName.DOC_IMPORTED,
        user,
        {"content_type": mime_types.DOCX},
    )


@patch("core.services.document_import_service.Converter.convert")
def test_import_without_identity_keeps_legacy_behavior(mock_convert, settings, import_user):
    """Clients that do not provide an identity keep the historical behavior."""
    settings.CONVERSION_UPLOAD_ENABLED = True
    mock_convert.return_value = CONVERTED_YJS
    user, client = import_user

    response = post_import(client, None)

    assert response.status_code == 201
    assert models.Document.objects.count() == 1
    assert models.DocumentImport.objects.count() == 0
    assert owner_access_count(user) == 1


@pytest.mark.django_db(transaction=True)
@patch("core.services.document_import_service.Converter.convert")
def test_import_root_concurrent_submitted_once(mock_convert, settings, import_user):
    """Two concurrent requests with the same identity convert and create once."""
    settings.CONVERSION_UPLOAD_ENABLED = True
    user, _client = import_user
    import_id = uuid4()

    entered = threading.Event()

    def convert_side_effect(_data, _content_type, _accept):
        entered.set()
        # Give the second request time to reach the advisory lock.
        time.sleep(0.5)
        return CONVERTED_YJS

    mock_convert.side_effect = convert_side_effect

    def submit():
        client = APIClient()
        client.force_login(user)
        try:
            return post_import(client, import_id)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        future1 = executor.submit(submit)
        # Ensure the first request has entered conversion before firing the
        # concurrent duplicate.
        assert entered.wait(timeout=5)
        future2 = executor.submit(submit)
        response1 = future1.result()
        response2 = future2.result()

    assert response1.status_code == 201
    assert response2.status_code == 200
    assert response1.json()["id"] == response2.json()["id"] == str(import_id)

    assert models.Document.objects.filter(id=import_id).count() == 1
    assert (
        models.DocumentAccess.objects.filter(
            document_id=import_id, user=user, role="owner"
        ).count()
        == 1
    )
    mock_convert.assert_called_once()


@patch("core.services.document_import_service.Converter.convert")
def test_import_same_identity_different_file_rejected(
    mock_convert, settings, import_user
):
    """The same identity with different file content is explicitly rejected."""
    settings.CONVERSION_UPLOAD_ENABLED = True
    mock_convert.return_value = CONVERTED_YJS
    _user, client = import_user
    import_id = uuid4()

    response1 = post_import(client, import_id)
    assert response1.status_code == 201

    response2 = post_import(client, import_id, file_obj=make_file(b"different bytes"))
    assert response2.status_code == 409

    assert models.Document.objects.count() == 1
    mock_convert.assert_called_once()


@patch("core.services.document_import_service.Converter.convert")
def test_import_same_identity_different_filename_rejected(
    mock_convert, settings, import_user
):
    """The same identity with a different file name is explicitly rejected."""
    settings.CONVERSION_UPLOAD_ENABLED = True
    mock_convert.return_value = CONVERTED_YJS
    _user, client = import_user
    import_id = uuid4()

    response1 = post_import(client, import_id)
    assert response1.status_code == 201

    response2 = post_import(
        client, import_id, file_obj=make_file(FILE_CONTENT, "another.docx")
    )
    assert response2.status_code == 409
    assert models.Document.objects.count() == 1


@patch("core.services.document_import_service.Converter.convert")
def test_import_same_identity_different_parent_rejected(
    mock_convert, settings, import_user
):
    """The same identity cannot be reused against a different target parent."""
    settings.CONVERSION_UPLOAD_ENABLED = True
    mock_convert.return_value = CONVERTED_YJS
    user, client = import_user
    import_id = uuid4()

    response_root = post_import(client, import_id)
    assert response_root.status_code == 201

    parent = factories.DocumentFactory(creator=user, users=[(user, "owner")])
    response_child = post_import(client, import_id, parent_id=parent.id)
    assert response_child.status_code == 409

    # No child was created under the parent.
    parent.refresh_from_db()
    assert parent.get_children().count() == 0
    assert models.Document.objects.filter(id=import_id).count() == 1


@patch("core.services.document_import_service.Converter.convert")
def test_import_retry_after_conversion_failure(mock_convert, settings, import_user):
    """A conversion failure leaves a reusable record: the same file retries safely."""
    settings.CONVERSION_UPLOAD_ENABLED = True
    mock_convert.side_effect = [
        ConversionError("boom"),
        CONVERTED_YJS,
    ]
    user, client = import_user
    import_id = uuid4()

    with patch("core.api.viewsets.posthog_capture") as mock_capture:
        response1 = post_import(client, import_id)
        assert response1.status_code == 400
        assert response1.json() == {"file": ["Could not convert file content"]}

        # The failure left no document but a fingerprint record in PROCESSING.
        assert models.Document.objects.filter(id=import_id).count() == 0
        record = models.DocumentImport.objects.get(id=import_id)
        assert record.status == models.DocumentImportStatusChoices.PROCESSING

        response2 = post_import(client, import_id)
        assert response2.status_code == 201

    document = models.Document.objects.get(id=import_id)
    assert document.content == CONVERTED_YJS
    assert document.accesses.filter(role="owner", user=user).exists()
    assert mock_convert.call_count == 2

    record.refresh_from_db()
    assert record.status == models.DocumentImportStatusChoices.COMPLETED
    assert record.document == document

    # Analytics fire only for the successful attempt.
    assert mock_capture.call_count == 2


@patch("core.services.document_import_service.Converter.convert")
def test_import_retry_after_crash_with_stored_content(
    mock_convert, settings, import_user
):
    """
    Crash after conversion + content storage but before the document row:
    the retry resumes without running the conversion again.
    """
    settings.CONVERSION_UPLOAD_ENABLED = True
    user, client = import_user
    import_id = uuid4()

    models.DocumentImport.objects.create(
        id=import_id,
        creator=user,
        parent=None,
        filename=FILENAME,
        file_hash=fingerprint(),
        content_type=mime_types.DOCX,
        status=models.DocumentImportStatusChoices.PROCESSING,
    )
    # Simulate converted content already persisted at the deterministic key.
    _store_imported_content(import_id, CONVERTED_YJS)

    response = post_import(client, import_id)

    assert response.status_code == 201
    mock_convert.assert_not_called()
    document = models.Document.objects.get(id=import_id)
    assert document.content == CONVERTED_YJS
    assert document.title == FILENAME
    assert document.accesses.filter(role="owner", user=user).exists()

    record = models.DocumentImport.objects.get(id=import_id)
    assert record.status == models.DocumentImportStatusChoices.COMPLETED
    assert record.document == document


@patch("core.services.document_import_service.Converter.convert")
def test_import_retry_heals_ownerless_document(
    mock_convert, settings, import_user
):
    """
    Crash after the document row commit but before the owner relation:
    the retry completes the missing owner access and returns the document.
    """
    settings.CONVERSION_UPLOAD_ENABLED = True
    user, client = import_user
    import_id = uuid4()

    document = factories.DocumentFactory(
        id=import_id, creator=user, title=FILENAME
    )
    assert document.accesses.exists() is False

    models.DocumentImport.objects.create(
        id=import_id,
        creator=user,
        parent=None,
        document=document,
        filename=FILENAME,
        file_hash=fingerprint(),
        content_type=mime_types.DOCX,
        status=models.DocumentImportStatusChoices.PROCESSING,
    )

    response = post_import(client, import_id)

    assert response.status_code == 200
    assert response.json()["id"] == str(import_id)
    mock_convert.assert_not_called()
    assert models.Document.objects.filter(id=import_id).count() == 1
    assert document.accesses.filter(role="owner", user=user).count() == 1

    record = models.DocumentImport.objects.get(id=import_id)
    assert record.status == models.DocumentImportStatusChoices.COMPLETED


@patch("core.services.document_import_service.Converter.convert")
def test_import_identity_of_unrelated_document_rejected(
    mock_convert, settings, import_user
):
    """An identity matching an existing document without import record is refused."""
    settings.CONVERSION_UPLOAD_ENABLED = True
    user, client = import_user
    foreign_document = factories.DocumentFactory()

    response = post_import(client, foreign_document.id)

    assert response.status_code == 409
    mock_convert.assert_not_called()
    # No owner access was granted on the foreign document.
    assert not foreign_document.accesses.filter(user=user).exists()
    assert models.DocumentImport.objects.filter(id=foreign_document.id).count() == 0


@patch("core.services.document_import_service.Converter.convert")
def test_import_child_replays_same_identity(mock_convert, settings, import_user):
    """Child imports replay the original child and keep permission inheritance."""
    settings.CONVERSION_UPLOAD_ENABLED = True
    mock_convert.return_value = CONVERTED_YJS
    user, client = import_user
    import_id = uuid4()
    parent = factories.DocumentFactory(creator=user, users=[(user, "owner")])

    response1 = post_import(client, import_id, parent_id=parent.id)
    response2 = post_import(client, import_id, parent_id=parent.id)

    assert response1.status_code == 201
    assert response2.status_code == 200
    assert response1.json()["id"] == response2.json()["id"] == str(import_id)

    parent.refresh_from_db()
    children = parent.get_children()
    assert children.count() == 1
    child = children.get()
    assert child.id == import_id
    assert child.title == FILENAME
    assert child.content == CONVERTED_YJS
    # Children inherit permissions: no direct access row is created.
    assert child.accesses.exists() is False

    mock_convert.assert_called_once()

    record = models.DocumentImport.objects.get(id=import_id)
    assert record.parent_id == parent.id
    assert record.status == models.DocumentImportStatusChoices.COMPLETED


@patch("core.services.document_import_service.Converter.convert")
def test_import_completed_record_replays_after_response_lost(
    mock_convert, settings, import_user
):
    """A COMPLETED record replays even when the original response never landed."""
    settings.CONVERSION_UPLOAD_ENABLED = True
    mock_convert.return_value = CONVERTED_YJS
    _user, client = import_user
    import_id = uuid4()

    assert post_import(client, import_id).status_code == 201
    assert post_import(client, import_id).status_code == 200
    assert post_import(client, import_id).status_code == 200

    assert models.Document.objects.filter(id=import_id).count() == 1
    assert mock_convert.call_count == 1
