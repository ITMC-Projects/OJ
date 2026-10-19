import hmac
import json
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_POST

from judge.auth.ots_backend import limit_linked_attempt
from judge.models import OTSAccountLink
from judge.services.ots_account_sync import OTSAccountSyncService, SyncError
from judge.services.ots_password import OTSPasswordClient


@csrf_exempt  # Service authentication only; browser password forms retain CSRF.
@require_POST
@sensitive_post_parameters()
@sensitive_variables()
def password_changed(request):
    expected = settings.OTS_CALLBACK_TOKEN
    supplied = request.headers.get('X-Service-Token', '')
    if len(expected) < 32 or not hmac.compare_digest(expected.encode(), supplied.encode()):
        return HttpResponse(status=401)
    body = request.read(2049)
    if len(body) > 2048:
        return HttpResponse(status=413)
    try:
        data = json.loads(body)
        uuid.UUID(data['eventId'])
        user_id = uuid.UUID(data['userId'])
        version = data['credentialVersion']
        occurred = parse_datetime(data['occurredAt'])
        if (type(version) is not int or version < 1 or occurred is None or timezone.is_naive(occurred) or
                occurred > timezone.now() + timedelta(minutes=5)):
            return HttpResponse(status=400)
    except (ValueError, KeyError, TypeError, AttributeError):
        return HttpResponse(status=400)
    link = OTSAccountLink.objects.filter(ots_user_id=user_id).first()
    # Version is a durable replay barrier; reconciliation creates missing links.
    if link is None or version <= link.source_credential_version:
        return HttpResponse(status=204)
    limit_linked_attempt('callback:' + str(user_id))
    try:
        item = OTSPasswordClient().credentials(user_id)
        if item['credentialVersion'] < version:
            return HttpResponse(status=409)
        OTSAccountSyncService().sync_account(item)
    except (SyncError, DatabaseError):
        return HttpResponse(status=503)
    return HttpResponse(status=204)
