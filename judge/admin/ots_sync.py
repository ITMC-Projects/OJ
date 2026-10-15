from django import forms
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db import transaction

from judge.models import OTSAccountLink, OTSAccountSyncRun


class SuperuserOnlyAdmin(admin.ModelAdmin):
    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False


class LinkForm(forms.ModelForm):
    identity_verified = forms.BooleanField(
        required=True, label='Đã xác minh UUID OTS và danh tính tài khoản OJ')

    class Meta:
        model = OTSAccountLink
        fields = '__all__'


@admin.register(OTSAccountLink)
class OTSAccountLinkAdmin(SuperuserOnlyAdmin):
    form = LinkForm
    list_display = ('user', 'ots_user_id', 'ots_student_id', 'ots_status', 'local_disabled', 'last_synced_at')
    search_fields = ('user__username', 'ots_student_id', 'ots_user_id')
    readonly_fields = ('last_synced_at', 'created_at')

    def get_readonly_fields(self, request, obj=None):
        if obj:
            return self.readonly_fields + ('user', 'ots_user_id', 'ots_student_id', 'ots_status', 'source_updated_at')
        return self.readonly_fields

    def save_model(self, request, obj, form, change):
        if not request.user.is_superuser:
            raise PermissionDenied
        with transaction.atomic():
            if change:
                current = OTSAccountLink.objects.select_for_update().get(pk=obj.pk)
                current.local_disabled = obj.local_disabled
                current.save(update_fields=['local_disabled'])
                obj = current
            else:
                super().save_model(request, obj, form, change)
            user = type(obj.user).objects.select_for_update().get(pk=obj.user_id)
            user.is_active = obj.ots_status == 'ACTIVE' and not obj.local_disabled
            user.save(update_fields=['is_active'])


@admin.register(OTSAccountSyncRun)
class OTSAccountSyncRunAdmin(SuperuserOnlyAdmin):
    list_display = ('id', 'status', 'triggered_by', 'created_count', 'updated_count', 'failed_count', 'created_at')
    readonly_fields = tuple(field.name for field in OTSAccountSyncRun._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
