from django.core.management.base import BaseCommand

from files.models import Document
from files.views import _generate_thumbnail, _thumb_key


class Command(BaseCommand):
    help = (
        "Pre-generate cached thumbnails for image documents, one at a time. The "
        "browse grid generates thumbnails on first view, but a folder of many "
        "photos fires dozens of generations at once, which can exhaust memory on "
        "a small instance. Running this sequentially up front keeps the live grid "
        "to cheap cache hits. Idempotent: existing thumbnails are skipped, so it "
        "is safe to re-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Regenerate even if a thumbnail already exists.",
        )

    def handle(self, *args, **options):
        from django.core.files.storage import default_storage

        force = options["force"]
        made = skipped = failed = 0
        for doc in Document.objects.order_by("pk").iterator():
            if doc.kind != "image":
                continue
            key = _thumb_key(doc)
            if not force and default_storage.exists(key):
                skipped += 1
                continue
            try:
                if force and default_storage.exists(key):
                    default_storage.delete(key)
                _generate_thumbnail(doc, key)
                made += 1
                self.stdout.write(f"  thumb: {doc.name}")
            except Exception as exc:  # noqa: BLE001 - report and keep going
                failed += 1
                self.stderr.write(self.style.WARNING(f"  failed {doc.name}: {exc}"))
        self.stdout.write(
            self.style.SUCCESS(
                f"Generated {made}, skipped {skipped} existing, {failed} failed."
            )
        )
