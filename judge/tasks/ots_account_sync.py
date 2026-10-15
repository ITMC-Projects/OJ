import logging

from celery import shared_task
from django.conf import settings

from judge.services.ots_account_sync import OTSAccountSyncService, SyncError, _reserve_sync

__all__ = ('sync_ots_accounts', 'auto_sync_ots_accounts')


@shared_task(ignore_result=True)
def sync_ots_accounts(run_id):
    OTSAccountSyncService().run(run_id)


@shared_task(ignore_result=True)
def auto_sync_ots_accounts():
    if not settings.OTS_SYNC_AUTO_ENABLED:
        return
    service = OTSAccountSyncService()
    try:
        service.validate_configuration()
        run = _reserve_sync()
    except SyncError as error:
        if str(error) == 'SYNC_ALREADY_RUNNING':
            return
        # Configuration failures remain visible without exposing credentials
        # or creating thousands of failed history rows.
        logging.getLogger('judge.ots_sync').warning('Automatic OTS sync skipped: %s', str(error))
        return
    service.run(run.pk)
