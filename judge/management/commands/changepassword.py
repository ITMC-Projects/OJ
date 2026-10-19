import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.management.commands.changepassword import Command as DjangoCommand
from django.core.management.base import CommandError

from judge.models import OTSAccountLink


class Command(DjangoCommand):
    def handle(self, *args, **options):
        username = options.get('username') or getpass.getuser()
        if OTSAccountLink.objects.using(options.get('database', 'default')).filter(
                **{'user__' + get_user_model().USERNAME_FIELD: username}).exists():
            raise CommandError('Linked account passwords must be changed through OTS.')
        return super().handle(*args, **options)
