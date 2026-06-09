import os
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from files.views import CHUNK_STAGING_DIR


class Command(BaseCommand):
    help = (
        "Delete abandoned chunked-upload .part files (an upload the browser "
        "started but never finalized) older than --hours. Run periodically so "
        "interrupted large uploads don't accumulate on the persistent disk."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--hours",
            type=float,
            default=24.0,
            help="Remove staging files last modified more than this many hours ago.",
        )

    def handle(self, *args, **options):
        root = os.path.join(settings.MEDIA_ROOT, CHUNK_STAGING_DIR)
        cutoff = time.time() - options["hours"] * 3600
        removed = 0
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                        removed += 1
                except OSError:
                    pass
        self.stdout.write(self.style.SUCCESS(f"Removed {removed} stale chunk file(s)."))
