from django.core.management.base import BaseCommand

from files.views import _sweep_stale_chunks


class Command(BaseCommand):
    help = (
        "Delete abandoned chunked-upload .part files (an upload the browser "
        "started but never finalized) older than --hours. Runs the same sweep "
        "the app performs opportunistically; useful at deploy time or manually."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--hours",
            type=float,
            default=24.0,
            help="Remove staging files last modified more than this many hours ago.",
        )

    def handle(self, *args, **options):
        removed = _sweep_stale_chunks(options["hours"] * 3600)
        self.stdout.write(self.style.SUCCESS(f"Removed {removed} stale chunk file(s)."))
