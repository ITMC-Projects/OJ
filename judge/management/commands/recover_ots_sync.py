from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from judge.models import OTSAccountSyncLock


class Command(BaseCommand):
    help = 'Release an interrupted OTS run only after its Celery worker has been stopped.'

    def add_arguments(self, parser):
        parser.add_argument('run_id', type=int)
        parser.add_argument('--worker-stopped', action='store_true')

    def handle(self, *args, **options):
        if not options['worker_stopped']:
            raise CommandError('Stop the worker first, then acknowledge with --worker-stopped.')
        with transaction.atomic():
            lock = OTSAccountSyncLock.objects.select_for_update().get(pk=1)
            if lock.run_id != options['run_id']:
                raise CommandError('Run does not hold the sync lock.')
            run = lock.run
            run.status = 'PARTIAL' if sum((run.created_count, run.updated_count,
                                           run.unchanged_count, run.failed_count)) else 'FAILED'
            run.error_summary = 'WORKER_INTERRUPTED'
            run.finished_at = timezone.now()
            run.save()
            lock.run = None
            lock.save(update_fields=['run'])
        self.stdout.write(self.style.SUCCESS('Sync lock released; a new run can reconcile all accounts.'))
