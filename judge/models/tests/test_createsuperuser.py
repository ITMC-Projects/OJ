from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from judge.forms import CustomAuthenticationForm
from judge.models import Profile


class CreateSuperuserTestCase(TestCase):
    def create_superuser(self):
        call_command('createsuperuser', username='profile_admin',
                     email='admin@example.com', interactive=False, stdout=StringIO())

    def test_creates_profile_and_allows_login(self):
        self.create_superuser()
        user = User.objects.get(username='profile_admin')
        self.assertTrue(user.is_superuser)
        self.assertIsNotNone(user.profile.language_id)
        CustomAuthenticationForm().confirm_login_allowed(user)
        self.assertEqual(Profile.objects.filter(user=user).count(), 1)

    def test_profile_failure_rolls_back_user(self):
        with patch('judge.management.commands.createsuperuser.Profile.objects.using',
                   side_effect=RuntimeError('profile creation failed')):
            with self.assertRaisesRegex(RuntimeError, 'profile creation failed'):
                self.create_superuser()
        self.assertFalse(User.objects.filter(username='profile_admin').exists())
        # A failed command must also disconnect its temporary signal handler.
        user = User.objects.create_user(username='ordinary_user')
        self.assertFalse(Profile.objects.filter(user=user).exists())
