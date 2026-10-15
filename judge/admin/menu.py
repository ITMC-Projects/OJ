from django.urls import reverse

from wpadmin.menu.custom import CustomModelLeftMenuWithDashboard
from wpadmin.menu.items import MenuItem


class MonitorLeftMenu(CustomModelLeftMenuWithDashboard):
    def init_with_context(self, context):
        super().init_with_context(context)
        user = context['request'].user
        if user.is_active and user.is_staff and user.has_perms((
            'judge.view_judge', 'judge.view_all_submission',
        )):
            self.children.insert(1, MenuItem(
                title='System monitoring',
                url=reverse('admin_monitor'),
                icon='fa-heartbeat',
                description='System monitoring',
            ))
