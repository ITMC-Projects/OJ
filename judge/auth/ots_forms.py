import logging
import uuid

from django import forms
from django.conf import settings
from django.contrib.auth.forms import PasswordResetForm, SetPasswordForm
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.views.decorators.debug import sensitive_variables

from judge.auth.ots_backend import limit_linked_attempt
from judge.models import OTSAccountLink
from judge.services.ots_account_sync import OTSAccountSyncService, SyncError
from judge.services.ots_password import OTSPasswordClient


class OTSChangePasswordForm(forms.Form):
    old_password = forms.CharField(label='Mật khẩu hiện tại', strip=False, max_length=72,
                                   widget=forms.PasswordInput(attrs={'autocomplete': 'current-password'}))
    new_password1 = forms.CharField(label='Mật khẩu mới', strip=False, max_length=72,
                                    widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}))
    new_password2 = forms.CharField(label='Xác nhận mật khẩu mới', strip=False, max_length=72,
                                    widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}))

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean(self):
        data = super().clean()
        if data.get('new_password1') != data.get('new_password2'):
            self.add_error('new_password2', 'Mật khẩu xác nhận không khớp.')
        # OTS validates its own policy. Do not run Django's independent validators.
        return data

    @sensitive_variables()
    def save(self, commit=True):
        link = OTSAccountLink.objects.get(user=self.user)
        if not settings.OTS_PASSWORD_CHANGE_ENABLED or link.local_disabled or not self.user.is_active:
            raise SyncError('USER_BLOCKED')
        limit_linked_attempt(self.user.pk)
        client = OTSPasswordClient()
        operation = uuid.uuid4()
        result = client.change(link.ots_user_id, self.cleaned_data['old_password'],
                               self.cleaned_data['new_password1'], operation)
        if (result.get('operationId') != str(operation) or type(result.get('credentialVersion')) is not int or
                result['credentialVersion'] < 1):
            raise SyncError('PASSWORD_OUTCOME_UNKNOWN')
        # Confirmation already means success. Outbox/reconciliation repairs a
        # failed cache refresh; never tell the user to repeat a committed change.
        try:
            OTSAccountSyncService().sync_account(client.credentials(link.ots_user_id))
        except (SyncError, DatabaseError):
            logging.getLogger(__name__).warning('OTS password committed; cache reconciliation pending')
        return self.user


class OTSPasswordResetForm(PasswordResetForm):
    def get_users(self, email):
        for user in super().get_users(email):
            if not OTSAccountLink.objects.filter(user=user).exists():
                yield user


class OTSSetPasswordForm(SetPasswordForm):
    def clean(self):
        cleaned = super().clean()
        if OTSAccountLink.objects.filter(user=self.user).exists():
            raise ValidationError('Tài khoản OTS phải đổi mật khẩu tại OTS.')
        return cleaned

    def save(self, commit=True):
        if OTSAccountLink.objects.filter(user=self.user).exists():
            raise ValidationError('Tài khoản OTS phải đổi mật khẩu tại OTS.')
        return super().save(commit=commit)
