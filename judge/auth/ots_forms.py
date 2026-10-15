from django.contrib.auth.forms import PasswordResetForm, SetPasswordForm
from django.core.exceptions import ValidationError

from judge.models import OTSAccountLink


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
