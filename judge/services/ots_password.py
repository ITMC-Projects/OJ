"""OTS owns linked credentials. Never retry a password mutation implicitly."""
import json
import uuid

import requests
from django.conf import settings
from django.views.decorators.debug import sensitive_variables

from judge.services.ots_account_sync import OTSAccountSyncService, SyncError


class OTSPasswordClient:
    @sensitive_variables()
    def call(self, user_id, action, payload=None, operation=None, read=False):
        user_id = str(uuid.UUID(str(user_id)))
        config = OTSAccountSyncService()
        config.url = settings.OTS_PASSWORD_API_URL.rstrip('/')
        config.token = settings.OTS_CREDENTIAL_READ_TOKEN if read else settings.OTS_PASSWORD_SERVICE_TOKEN
        config.timeout = settings.OTS_PASSWORD_TIMEOUT_SECONDS
        config.validate_configuration()
        headers = {'X-Service-Token': config.token}
        if operation:
            headers['Idempotency-Key'] = str(uuid.UUID(str(operation)))
        try:
            with requests.Session() as session:
                session.trust_env = False
                with session.request('GET' if payload is None else 'POST',
                                     config.url + '/internal/oj/users/' + user_id + '/' + action,
                                     headers=headers, json=payload, timeout=(config.timeout, config.timeout),
                                     allow_redirects=False, stream=True) as response:
                    content = bytearray()
                    for chunk in response.iter_content(4096):
                        content.extend(chunk)
                        if len(content) > 16384:
                            raise SyncError('INVALID_RESPONSE')
                    try:
                        data = json.loads(content)
                    except (ValueError, UnicodeError):
                        raise SyncError('INVALID_RESPONSE') from None
                    if response.status_code != 200:
                        code = data.get('code') if isinstance(data, dict) else None
                        if code not in ('CURRENT_PASSWORD_INVALID', 'USER_BLOCKED', 'USER_NOT_FOUND',
                                        'INVALID_PASSWORD', 'RATE_LIMITED', 'INVALID_SERVICE_TOKEN'):
                            code = 'OTS_UNAVAILABLE'
                        raise SyncError(code)
                    if not isinstance(data, dict):
                        raise SyncError('INVALID_RESPONSE')
                    return data
        except requests.RequestException:
            raise SyncError('OTS_UNAVAILABLE') from None

    @sensitive_variables()
    def credentials(self, user_id):
        item = self.call(user_id, 'credentials', read=True)
        if (item.get('id') != str(user_id) or type(item.get('credentialVersion')) is not int or
                item['credentialVersion'] < 1):
            raise SyncError('INVALID_RESPONSE')
        return item

    def state(self, user_id):
        data = self.call(user_id, 'credential-state', read=True)
        if (data.get('userId') != str(user_id) or type(data.get('credentialVersion')) is not int or
                data['credentialVersion'] < 1 or data.get('status') not in ('ACTIVE', 'BLOCKED')):
            raise SyncError('INVALID_RESPONSE')
        return data

    @sensitive_variables()
    def verify(self, user_id, password):
        data = self.call(user_id, 'password/verify', {'password': password})
        if (data.get('userId') != str(user_id) or type(data.get('credentialVersion')) is not int or
                data['credentialVersion'] < 1):
            raise SyncError('INVALID_RESPONSE')
        return data

    @sensitive_variables()
    def change(self, user_id, current, new, operation):
        try:
            return self.call(user_id, 'password/change', {'currentPassword': current, 'newPassword': new}, operation)
        except SyncError as error:
            if str(error) not in ('OTS_UNAVAILABLE', 'INVALID_RESPONSE'):
                raise
            # The server may have committed. Query its durable operation ledger;
            # never interpret a missing receipt during an in-flight call as failure.
            try:
                return self.call(user_id, 'password/operations/' + str(operation))
            except SyncError:
                raise SyncError('PASSWORD_OUTCOME_UNKNOWN') from None
