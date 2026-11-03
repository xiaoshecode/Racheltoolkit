from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from storage.models import PreviewAsset
from storage.previews import render_preview


class Command(BaseCommand):
    help = "Render pending private file previews in a worker. Run periodically outside the web process."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--retry-failed", action="store_true")
        parser.add_argument("--asset", type=int, help="Limit to one preview row")

    def handle(self, *args, **options):
        if options["limit"] <= 0:
            raise CommandError("limit must be positive")
        candidates = PreviewAsset.objects.all()
        if options["asset"] is not None:
            candidates = candidates.filter(pk=options["asset"])
        available = Q(state="pending") | Q(state="processing", started_at__lt=timezone.now() - timedelta(minutes=5))
        if options["retry_failed"]:
            available |= Q(state="failed")
        failed = 0
        for asset in candidates.filter(available).order_by("pk")[:options["limit"]]:
            if asset.state == "failed":
                candidates.filter(pk=asset.pk, state="failed").update(state="pending")
            asset = render_preview(asset)
            self.stdout.write(f"preview={asset.pk} state={asset.state} pages={len(asset.page_manifest)}")
            failed += asset.state == "failed"
        if failed:
            raise CommandError(f"{failed} preview(s) failed. Original files preserved.")
