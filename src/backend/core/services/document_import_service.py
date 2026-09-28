"""
Idempotent DOCX/Markdown document import orchestration.

A file import carries a client-provided identity (the target document UUID).
This module makes importing the same identity with the same file and target
parent replayable at both the root document and child document entries:

* the request fingerprint (creator, parent, filename, content type and file
  hash) is persisted before the expensive conversion runs;
* concurrent requests sharing an identity are serialized with a PostgreSQL
  advisory lock, so the conversion and the document creation run once;
* an identity reused with a different file or a different target parent is
  explicitly rejected (HTTP 409);
* conversion failures and creation transaction rollbacks leave the record in
  PROCESSING state, from which a retry with the same file resumes;
* after a crash between the conversion, the content storage, the root
  document owner relation and the response delivery, the state is recovered
  from durable facts: the import record, the document row and the
  deterministic ``{document_id}/file`` object in storage.
"""

import contextlib
import hashlib
import logging

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, connection, transaction
from django.utils.translation import gettext_lazy as _

import rest_framework as drf
from botocore.exceptions import ClientError
from rest_framework import status

from core import models
from core.services import mime_types
from core.services.converter_services import Converter
from core.utils.treebeard import create_tree_node_with_retry

logger = logging.getLogger(__name__)

_UINT64_MASK = (1 << 64) - 1
_SIGNED_64_THRESHOLD = 1 << 63
_SIGNED_64_OFFSET = 1 << 64


class ImportConflictError(drf.exceptions.APIException):
    """Raised when an import identity is reused with different parameters."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = _(
        "This import identity was already used with a different file or target parent."
    )
    default_code = "import_conflict"


def compute_file_fingerprint(filename, content_type, file_bytes):
    """Return the SHA-256 fingerprint of an uploaded file."""
    digest = hashlib.sha256()
    digest.update((filename or "").encode("utf-8"))
    digest.update(b"\x00")
    digest.update((content_type or "").encode("utf-8"))
    digest.update(b"\x00")
    digest.update(file_bytes)
    return digest.hexdigest()


def _advisory_lock_key(lock_uuid):
    """Fold a 128-bit UUID into a signed 64-bit advisory lock key."""
    value = ((lock_uuid.int >> 64) ^ (lock_uuid.int & _UINT64_MASK)) & _UINT64_MASK
    if value >= _SIGNED_64_THRESHOLD:
        return value - _SIGNED_64_OFFSET
    return value


@contextlib.contextmanager
def advisory_lock_for_import(lock_uuid):
    """
    Serialize requests sharing an import identity.

    Session-level advisory locks are automatically released by PostgreSQL
    when the database connection closes, so a crashed worker never holds the
    lock: a retry acquires it and resumes from the durable state.
    """
    lock_key = _advisory_lock_key(lock_uuid)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_lock(%s)", [lock_key])
        try:
            yield
        finally:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [lock_key])


def _content_file_key(document_id):
    """Object storage key at which the converted content of a document lives."""
    return f"{document_id!s}/file"


def imported_content_exists(document_id):
    """Return True when converted content is already stored for the identity."""
    try:
        default_storage.connection.meta.client.head_object(
            Bucket=default_storage.bucket_name,
            Key=_content_file_key(document_id),
        )
    except ClientError as excpt:
        if excpt.response["Error"]["Code"] == "404":
            return False
        raise
    return True


def _store_imported_content(document_id, converted_content):
    """Persist converted content at the deterministic key before the DB commit."""
    default_storage.save(
        _content_file_key(document_id),
        ContentFile(converted_content.encode("utf-8")),
    )


def _ensure_record_matches(record, *, creator, parent, fingerprint):
    """Reject identities reused with a different file or target parent."""
    filename, content_type, file_hash = fingerprint
    if (
        record.creator_id != creator.id
        or record.parent_id != (parent.id if parent is not None else None)
        or record.filename != filename
        or record.file_hash != file_hash
        or record.content_type != (content_type or "")
    ):
        logger.info(
            "import identity %s reused with conflicting parameters", record.id
        )
        raise ImportConflictError()


def _register_import(*, creator, parent, document_id, fingerprint):
    """
    Persist the import fingerprint before conversion (short transaction).

    Returns the record and whether it was just created. A record is kept on
    conversion failure or creation rollback so that retries are allowed with
    the same file while a different file is rejected.
    """
    filename, content_type, file_hash = fingerprint
    with transaction.atomic():
        record = (
            models.DocumentImport.objects.select_for_update()
            .filter(id=document_id)
            .first()
        )
        if record is None:
            try:
                record = models.DocumentImport.objects.create(
                    id=document_id,
                    creator=creator,
                    parent=parent,
                    document=None,
                    filename=filename,
                    file_hash=file_hash,
                    content_type=content_type or "",
                    status=models.DocumentImportStatusChoices.PROCESSING,
                )
            except IntegrityError:
                # A concurrent request may have won the insert race: re-read
                # the row instead of failing.
                record = (
                    models.DocumentImport.objects.select_for_update()
                    .filter(id=document_id)
                    .first()
                )
                if record is None:
                    raise
                _ensure_record_matches(
                    record,
                    creator=creator,
                    parent=parent,
                    fingerprint=fingerprint,
                )
                return record, False

            # A record is only legitimately created before the document. If a
            # document already exists without any import record, the identity
            # belongs to an unrelated document and must not be claimed.
            if models.Document.objects.filter(id=document_id).exists():
                raise ImportConflictError()
            return record, True

        _ensure_record_matches(
            record,
            creator=creator,
            parent=parent,
            fingerprint=fingerprint,
        )
        return record, False


def _create_document_node(*, parent, document_id, filename, node_kwargs, creator):
    """Create the root or child document node inside the caller transaction."""
    attributes = {
        **node_kwargs,
        "id": document_id,
        "title": filename,
        "creator": creator,
    }
    if parent is None:
        return create_tree_node_with_retry(
            lambda: models.Document.add_root(**attributes)
        )
    return create_tree_node_with_retry(lambda: parent.add_child(**attributes))


def import_document(*, creator, parent, uploaded_file, document_id, node_kwargs):
    """
    Import an uploaded file idempotently.

    Returns ``(document, replayed)`` where ``replayed`` is True when the
    import had already completed and the original document is returned.
    """
    file_bytes = uploaded_file.read()
    filename = uploaded_file.name
    content_type = uploaded_file.content_type or ""
    fingerprint = (
        filename,
        content_type,
        compute_file_fingerprint(filename, content_type, file_bytes),
    )

    record, _created = _register_import(
        creator=creator,
        parent=parent,
        document_id=document_id,
        fingerprint=fingerprint,
    )

    # The import already completed before (e.g. the response was lost):
    # return the original document without converting again.
    if (
        record.status == models.DocumentImportStatusChoices.COMPLETED
        and record.document_id is not None
    ):
        return record.document, True

    with advisory_lock_for_import(document_id):
        # Re-read after acquiring the lock: a concurrent request may have
        # completed the import while we were waiting.
        record = models.DocumentImport.objects.get(id=document_id)
        _ensure_record_matches(
            record,
            creator=creator,
            parent=parent,
            fingerprint=fingerprint,
        )
        if (
            record.status == models.DocumentImportStatusChoices.COMPLETED
            and record.document_id is not None
        ):
            return record.document, True

        document = models.Document.objects.filter(id=document_id).first()

        if document is None:
            # Either no work was ever done, or the process crashed after the
            # conversion and the content storage but before the document row
            # was committed. Stored content lets us skip the expensive
            # conversion in the latter case.
            if not imported_content_exists(document_id):
                converted_content = Converter().convert(
                    file_bytes,
                    content_type=content_type,
                    accept=mime_types.YJS,
                )
                _store_imported_content(document_id, converted_content)

            with transaction.atomic():
                document = _create_document_node(
                    parent=parent,
                    document_id=document_id,
                    filename=filename,
                    node_kwargs=node_kwargs,
                    creator=creator,
                )
                if parent is None:
                    # Root documents own themselves: commit the owner relation
                    # in the same transaction as the document row so that no
                    # ownerless half-finished document can ever be visible.
                    models.DocumentAccess.objects.get_or_create(
                        document=document,
                        user=creator,
                        defaults={"role": models.RoleChoices.OWNER},
                    )
                record.document = document
                record.status = models.DocumentImportStatusChoices.COMPLETED
                record.save(
                    update_fields=["document", "status", "updated_at"]
                )

            return document, False

        # The document row exists but the record is still PROCESSING: the
        # process crashed during creation. Complete the missing steps instead
        # of creating a duplicate.
        with transaction.atomic():
            if parent is None:
                models.DocumentAccess.objects.get_or_create(
                    document=document,
                    user=creator,
                    defaults={"role": models.RoleChoices.OWNER},
                )
            record.document = document
            record.status = models.DocumentImportStatusChoices.COMPLETED
            record.save(update_fields=["document", "status", "updated_at"])

        logger.info("import %s recovered from a half-finished state", document_id)
        return document, True


def resolve_import_identity(validated_data):
    """
    Extract the replayable identity and safe node attributes from validated
    serializer data.

    Returns ``(document_id, node_kwargs)`` or ``(None, node_kwargs)`` when the
    request does not carry an identity (legacy clients).
    """
    node_kwargs = dict(validated_data)
    node_kwargs.pop("file", None)
    node_kwargs.pop("websocket", None)
    document_id = node_kwargs.pop("id", None)
    # The filename always wins over any provided title, mirroring the legacy
    # conversion behavior; the view sets it from the uploaded file name.
    node_kwargs.pop("title", None)
    return document_id, node_kwargs
