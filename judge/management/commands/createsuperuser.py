from django.contrib.auth.management.commands.createsuperuser import Command as DjangoCommand
from django.db import transaction
from django.db.models.signals import post_save

from judge.models import Profile


class Command(DjangoCommand):
    def handle(self, *args, **options):
        database = options['database']

        def create_profile(sender, instance, created, using, raw=False, **kwargs):
            if created and not raw and using == database:
                Profile.objects.using(using).get_or_create(user=instance)

        # Django's command creates only auth.User. Keep profile creation in the
        # same transaction, without changing registration or social-auth flows.
        post_save.connect(create_profile, sender=self.UserModel)
        try:
            with transaction.atomic(using=database):
                return super().handle(*args, **options)
        finally:
            post_save.disconnect(create_profile, sender=self.UserModel)
