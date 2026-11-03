from django.core.management.base import BaseCommand, CommandError
from submissions.models import Submission
from submissions.services import reconcile


class Command(BaseCommand):
    help = "Report incomplete submissions; use --repair to reconcile immutable objects."

    def add_arguments(self, parser):
        parser.add_argument("--repair", action="store_true")
        parser.add_argument("--submission")
        parser.add_argument(
            "--writer-stopped",
            action="store_true",
            help="Operator confirmed the named worker and remote request have terminated",
        )

    def handle(self, **options):
        if options["writer_stopped"] and not (options["submission"] and options["repair"]):
            raise CommandError("--writer-stopped requires --submission and --repair")
        pending = Submission.objects.exclude(state__in=["confirmed", "quarantined"])
        if options["submission"]:
            pending = pending.filter(pk=options["submission"])
        for sub in list(pending):
            self.stdout.write(
                f"{sub.pk} {reconcile(sub, options['repair'], options['writer_stopped'])}"
            )
