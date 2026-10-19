from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError
from django.views.decorators.debug import sensitive_variables

from judge.models import OTSAccountLink
from judge.services.ots_account_sync import OTSAccountSyncService, SyncError
from judge.services.ots_password import OTSPasswordClient


def limit_linked_attempt(user_id):
    # Requires the shared production cache. Cache failure is fail-closed.
    key = 'ots:credential:attempt:' + str(user_id)
    try:
        if cache.add(key, 1, 60):
            return
        if cache.incr(key) <= 10:
            return
    except Exception:
        pass
    raise PermissionDenied('Account service temporarily unavailable')


class OTSModelBackend(ModelBackend):
    @sensitive_variables()
    def authenticate(self, request, username=None, password=None, **kwargs):
        username = username or kwargs.get(get_user_model().USERNAME_FIELD)
        link = OTSAccountLink.objects.select_related('user').filter(user__username=username).first()
        if link is None:
            return super().authenticate(request, username=username, password=password, **kwargs)
        limit_linked_attempt(link.user_id)
        if link.local_disabled or not self.user_can_authenticate(link.user):
            raise PermissionDenied('Account unavailable')
        try:
            client = OTSPasswordClient()
            verified = client.verify(link.ots_user_id, password)
            item = client.credentials(link.ots_user_id)
            if item['status'] != 'ACTIVE' or item['credentialVersion'] != verified['credentialVersion']:
                raise PermissionDenied('Credentials changed; please sign in again')
            OTSAccountSyncService().sync_account(item)
            link.refresh_from_db()
            user = get_user_model().objects.get(pk=link.user_id)
            if (link.source_credential_version != verified['credentialVersion'] or link.local_disabled or
                    not self.user_can_authenticate(user)):
                raise PermissionDenied('Account unavailable')
            return user
        except (SyncError, DatabaseError):
            # PermissionDenied terminates Django's backend chain: no stale cache
            # fallback, including IP authentication, after an OTS failure.
            raise PermissionDenied('Account service rejected authentication') from None
