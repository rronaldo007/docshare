import os

from django.conf import settings
from django.core.files import File
from django.core.files.storage import FileSystemStorage, storages
from django.core.management.base import BaseCommand, CommandError

from files.models import Document


class Command(BaseCommand):
    help = (
        "Copy every Document's bytes from the local disk (MEDIA_ROOT) into the "
        "configured object-storage bucket, preserving each file's storage key so "
        "no database rows change. Idempotent and resumable: files already present "
        "in the bucket are skipped, so it is safe to re-run after an interruption. "
        "Run this AFTER setting the DJANGO_S3_* env vars (so the default storage "
        "is the bucket) but while the local disk still holds the original files."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be copied without writing anything to the bucket.",
        )

    def handle(self, *args, **options):
        dest = storages["default"]
        # Refuse to run unless the default storage is actually the bucket; copying
        # disk -> disk would be a no-op that silently "succeeds".
        if "s3" not in type(dest).__module__.lower() and "s3" not in type(dest).__name__.lower():
            raise CommandError(
                "Default storage is not S3/object storage. Set DJANGO_S3_BUCKET "
                "(and the access key / secret / endpoint) before running this, so "
                "uploads and this migration both target the bucket."
            )

        # Source is the local disk explicitly, regardless of the (now S3) default.
        source = FileSystemStorage(location=settings.MEDIA_ROOT)
        dry = options["dry_run"]

        copied = skipped = missing = 0
        total_bytes = 0
        qs = Document.objects.order_by("pk").iterator()
        for doc in qs:
            name = doc.file.name
            if not name:
                continue
            if dest.exists(name):
                skipped += 1
                continue
            if not source.exists(name):
                self.stderr.write(self.style.WARNING(f"  missing on disk: {name}"))
                missing += 1
                continue

            size = source.size(name)
            if dry:
                self.stdout.write(f"  would copy: {name} ({size} bytes)")
                copied += 1
                total_bytes += size
                continue

            # Stream the file object straight up (boto3 multipart) -- never reads
            # the whole file into memory. Save under the SAME key; file_overwrite
            # is False, but we only reach here when the key is absent, so the key
            # is preserved and doc.file.name stays valid.
            with source.open(name, "rb") as fh:
                saved = dest.save(name, File(fh))
            if saved != name:
                # Should never happen (key was absent), but fail loud if storage
                # renamed it -- the DB would otherwise point at a missing key.
                raise CommandError(
                    f"Bucket renamed {name!r} to {saved!r}; aborting so the "
                    f"database is not left inconsistent."
                )
            copied += 1
            total_bytes += size
            self.stdout.write(f"  copied: {name} ({size} bytes)")

        verb = "Would copy" if dry else "Copied"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {copied} file(s) ({total_bytes} bytes); "
                f"skipped {skipped} already in bucket; {missing} missing on disk."
            )
        )
        if not dry and missing:
            self.stdout.write(
                "Note: missing-on-disk files have no bytes to migrate; their "
                "Document rows will 404 until re-uploaded."
            )
