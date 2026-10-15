from celery import shared_task

from judge.services.ots_account_sync import OTSAccountSyncService

__all__ = ('sync_ots_accounts',)


@shared_task(ignore_result=True)
def sync_ots_accounts(run_id):
    OTSAccountSyncService().run(run_id)
