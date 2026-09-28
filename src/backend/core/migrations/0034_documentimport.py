# Generated for idempotent DOCX/Markdown document imports

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0033_document_document_attachments_gin"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="DocumentImport",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        help_text="primary key for the record as UUID",
                        primary_key=True,
                        serialize=False,
                        verbose_name="id",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        auto_now_add=True,
                        editable=False,
                        help_text="date and time at which a record was created",
                        verbose_name="created on",
                    ),
                ),
                (
                    "updated_at",
                    models.DateTimeField(
                        auto_now=True,
                        editable=False,
                        help_text="date and time at which a record was last updated",
                        verbose_name="updated on",
                    ),
                ),
                ("filename", models.CharField(max_length=255, verbose_name="filename")),
                (
                    "file_hash",
                    models.CharField(max_length=64, verbose_name="file hash"),
                ),
                (
                    "content_type",
                    models.CharField(blank=True, max_length=255, verbose_name="content type"),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("processing", "Processing"),
                            ("completed", "Completed"),
                        ],
                        default="processing",
                        max_length=20,
                    ),
                ),
                (
                    "creator",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="document_imports",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="creator",
                    ),
                ),
                (
                    "parent",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="child_imports",
                        to="core.document",
                        verbose_name="parent document",
                    ),
                ),
                (
                    "document",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="import_records",
                        to="core.document",
                        verbose_name="imported document",
                    ),
                ),
            ],
            options={
                "verbose_name": "Document import",
                "verbose_name_plural": "Document imports",
                "db_table": "impress_document_import",
            },
        ),
        migrations.AddIndex(
            model_name="documentimport",
            index=models.Index(
                fields=["creator", "status"], name="document_import_creator"
            ),
        ),
    ]
