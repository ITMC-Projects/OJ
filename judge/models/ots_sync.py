from django.conf import settings
from django.db import models


class OTSAccountLink(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='ots_account_link')
    ots_user_id = models.UUIDField(unique=True)
    ots_student_id = models.CharField(max_length=30)
    ots_status = models.CharField(max_length=7, choices=[('ACTIVE', 'ACTIVE'), ('BLOCKED', 'BLOCKED')])
    local_disabled = models.BooleanField(default=False)
    source_updated_at = models.DateTimeField()
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return str(self.ots_user_id)


class OTSAccountSyncRun(models.Model):
    STATUSES = [(value, value) for value in ('QUEUED', 'RUNNING', 'SUCCEEDED', 'PARTIAL', 'FAILED')]
    status = models.CharField(max_length=9, choices=STATUSES, default='QUEUED')
    triggered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    total = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    unchanged_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    cursor = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_summary = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ['-id']


class OTSAccountSyncError(models.Model):
    run = models.ForeignKey(OTSAccountSyncRun, on_delete=models.CASCADE, related_name='errors')
    student_id = models.CharField(max_length=30, blank=True)
    code = models.CharField(max_length=40)


class OTSAccountSyncLock(models.Model):
    """Seeded singleton: reserves both queued and running jobs across all workers."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    run = models.OneToOneField(OTSAccountSyncRun, null=True, blank=True, on_delete=models.PROTECT)
