from django.contrib.auth import logout
from django.http import HttpResponse
from django.urls import reverse
from django.utils.crypto import constant_time_compare

from judge.models import OTSAccountLink
from judge.services.ots_account_sync import SyncError
from judge.services.ots_password import OTSPasswordClient


class OTSCredentialMiddleware:
    """Fail closed for linked sessions on every authenticated HTTP request.

    No network partition can guarantee instantaneous global revocation. This
    check prevents requests from proceeding while authority is unreachable.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated:
            link = OTSAccountLink.objects.filter(user=request.user).first()
            if link:
                # AdminSite's own password form is separate from UserAdmin's
                # target-user route and otherwise uses Django's local writer.
                if request.path_info == reverse('admin:password_change'):
                    return HttpResponse('Tài khoản liên kết phải đổi mật khẩu qua OTS.', status=403)
                try:
                    item = OTSPasswordClient().state(link.ots_user_id)
                except SyncError:
                    return HttpResponse('Dịch vụ tài khoản tạm thời không khả dụng.', status=503)
                if (link.local_disabled or item['status'] != 'ACTIVE' or
                        item['credentialVersion'] != link.source_credential_version or
                        not constant_time_compare(request.session.get('_auth_user_hash', ''),
                                                  request.user.get_session_auth_hash())):
                    logout(request)
                    return HttpResponse('Mật khẩu đã thay đổi. Vui lòng đăng nhập lại.', status=401)
        return self.get_response(request)
