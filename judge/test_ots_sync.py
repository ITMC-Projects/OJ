import io
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import bcrypt
import requests
from django.contrib.auth import authenticate
from django.contrib.auth.models import Permission, User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.exceptions import ValidationError
from django.db import connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.auth.ots_forms import OTSPasswordResetForm, OTSSetPasswordForm
from judge.auth.ots_password_hasher import OTSBcryptPasswordHasher, valid_source_hash
from judge.models import Language, OTSAccountLink, OTSAccountSyncLock, OTSAccountSyncRun, Profile
from judge.services.ots_account_sync import OTSAccountSyncService, SyncError, queue_sync


@override_settings(OTS_SYNC_API_URL='https://ots.example.test', OTS_SYNC_SERVICE_TOKEN='x' * 32,
                   OTS_SYNC_TIMEOUT_SECONDS=1, OTS_SYNC_PAGE_SIZE=100,
                   OTS_SYNC_ALLOW_INTERNAL_HTTP=False,
                   AUTHENTICATION_BACKENDS=['django.contrib.auth.backends.ModelBackend'])
class AccountSyncTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Language.objects.get_or_create(key='PY3', defaults={'name': 'Python 3', 'short_name': 'Python 3'})
        cls.raw_hash = bcrypt.hashpw(b'old-password', bcrypt.gensalt(rounds=4)).decode()

    def setUp(self):
        self.service = OTSAccountSyncService()
        self.item = {'id': str(uuid.uuid4()), 'studentId': 'n23dccn001', 'fullName': 'Nguyễn Văn A',
                     'email': 'student@example.test', 'role': 'USER', 'status': 'ACTIVE',
                     'passwordHash': self.raw_hash, 'updatedAt': timezone.now().isoformat()}
        OTSAccountSyncLock.objects.get_or_create(pk=1)

    def synced_user(self):
        return User.objects.get(ots_account_link__ots_user_id=self.item['id'])

    def test_create_repeat_update_and_identity_preserved(self):
        self.assertEqual(self.service.sync_account(self.item), 'created_count')
        user = self.synced_user()
        original_id = user.pk
        self.assertEqual(user.username, 'N23DCCN001')
        self.assertTrue(Profile.objects.filter(user=user).exists())
        self.assertFalse(user.is_staff or user.is_superuser)
        self.assertEqual(self.service.sync_account(self.item), 'unchanged_count')
        self.item.update(studentId='N23DCCN002', fullName='Tên mới', email='new@example.test')
        self.assertEqual(self.service.sync_account(self.item), 'updated_count')
        user = self.synced_user()
        self.assertEqual((user.pk, user.username, user.first_name, user.email),
                         (original_id, 'N23DCCN002', 'Tên mới', 'new@example.test'))

    def test_go_bcrypt_login_upgrade_and_next_sync_password_rotation(self):
        self.service.sync_account(self.item)
        user = self.synced_user()
        self.assertIsNotNone(authenticate(username=user.username, password='old-password'))
        user.refresh_from_db()
        self.assertTrue(user.password.startswith('pbkdf2_sha256$'))
        self.assertEqual(self.service.sync_account(self.item), 'updated_count')
        self.item['passwordHash'] = bcrypt.hashpw(b'new-password', bcrypt.gensalt(rounds=4)).decode()
        self.service.sync_account(self.item)
        self.assertIsNone(authenticate(username=user.username, password='old-password'))
        self.assertIsNotNone(authenticate(username=user.username, password='new-password'))

    def test_block_and_local_disabled(self):
        self.item['status'] = 'BLOCKED'
        self.service.sync_account(self.item)
        user = self.synced_user()
        self.assertFalse(user.is_active)
        self.assertIsNone(authenticate(username=user.username, password='old-password'))
        self.item['status'] = 'ACTIVE'
        self.service.sync_account(self.item)
        self.assertTrue(self.synced_user().is_active)
        OTSAccountLink.objects.filter(user=user).update(local_disabled=True)
        self.service.sync_account(self.item)
        self.assertFalse(self.synced_user().is_active)

    def test_existing_privileges_preserved(self):
        self.service.sync_account(self.item)
        User.objects.filter(pk=self.synced_user().pk).update(is_staff=True, is_superuser=True)
        self.service.sync_account(self.item)
        user = self.synced_user()
        self.assertTrue(user.is_staff and user.is_superuser)

    def test_unlinked_username_and_email_conflict_do_not_overwrite(self):
        user = User.objects.create_user('N23DCCN001', 'other@example.test', 'local-password')
        password = user.password
        with self.assertRaisesRegex(SyncError, 'USERNAME_CONFLICT'):
            self.service.sync_account(self.item)
        user.refresh_from_db()
        self.assertEqual(user.password, password)
        self.assertEqual(user.email, 'other@example.test')
        self.item['studentId'] = 'N23DCCN002'
        self.item['email'] = 'OTHER@example.test'
        with self.assertRaisesRegex(SyncError, 'EMAIL_CONFLICT'):
            self.service.sync_account(self.item)
        self.assertEqual(OTSAccountLink.objects.count(), 0)

    def test_linked_username_conflict_is_atomic(self):
        self.service.sync_account(self.item)
        User.objects.create_user('N23DCCN002')
        self.item.update(studentId='N23DCCN002', fullName='Should not save')
        with self.assertRaisesRegex(SyncError, 'USERNAME_CONFLICT'):
            self.service.sync_account(self.item)
        self.assertEqual(self.synced_user().first_name, 'Nguyễn Văn A')

    def test_profile_failure_rolls_back_user_and_link(self):
        with patch('judge.services.ots_account_sync.Profile.objects.get_or_create', side_effect=RuntimeError()):
            with self.assertRaisesRegex(SyncError, 'PROFILE_CREATE_FAILED'):
                self.service.sync_account(self.item)
        self.assertFalse(User.objects.filter(username='N23DCCN001').exists())
        self.assertFalse(OTSAccountLink.objects.exists())

    def test_invalid_account_leaves_database_untouched(self):
        for fields, code in [({'studentId': 'bad'}, 'INVALID_STUDENT_ID'),
                             ({'passwordHash': 'plaintext'}, 'INVALID_SOURCE_HASH'),
                             ({'passwordHash': self.raw_hash.replace('$04$', '$31$')}, 'INVALID_SOURCE_HASH'),
                             ({'role': 'ADMIN'}, 'SOURCE_DATA_INVALID'),
                             ({'status': 'UNKNOWN'}, 'SOURCE_DATA_INVALID'),
                             ({'email': 'bad'}, 'SOURCE_DATA_INVALID'),
                             ({'updatedAt': '2026-01-01T00:00:00'}, 'SOURCE_DATA_INVALID')]:
            with self.subTest(fields=list(fields)):
                item = {**self.item, **fields}
                with self.assertRaisesRegex(SyncError, code):
                    self.service.sync_account(item)
        self.assertFalse(OTSAccountLink.objects.exists())

    def test_invalid_hash_never_overwrites_linked_credentials(self):
        self.service.sync_account(self.item)
        before = self.synced_user().password
        self.item.update(passwordHash='invalid', fullName='Must not save')
        with self.assertRaisesRegex(SyncError, 'INVALID_SOURCE_HASH'):
            self.service.sync_account(self.item)
        user = self.synced_user()
        self.assertEqual(user.password, before)
        self.assertEqual(user.first_name, 'Nguyễn Văn A')

    def test_password_paths_block_linked_only(self):
        self.service.sync_account(self.item)
        user = self.synced_user()
        self.assertEqual(list(OTSPasswordResetForm().get_users(user.email)), [])
        form = OTSSetPasswordForm(user, {'new_password1': 'new-password', 'new_password2': 'new-password'})
        self.assertFalse(form.is_valid())
        with self.assertRaises(ValidationError):
            form.save()
        local = User.objects.create_user('local', 'local@example.test', 'local-password')
        self.assertEqual(list(OTSPasswordResetForm().get_users(local.email)), [local])
        client = Client()
        client.force_login(user)
        self.assertEqual(client.get(reverse('password_change')).status_code, 403)

    def test_bcrypt_variants_and_invalid_hash(self):
        hasher = OTSBcryptPasswordHasher()
        for prefix in ('$2a$', '$2b$', '$2y$'):
            raw = prefix + self.raw_hash[4:]
            self.assertTrue(valid_source_hash(raw))
            self.assertTrue(hasher.verify('old-password', 'ots_bcrypt$' + raw))
        self.assertFalse(hasher.verify('wrong', 'ots_bcrypt$' + self.raw_hash))
        self.assertFalse(hasher.verify('a' * 73, 'ots_bcrypt$' + self.raw_hash))
        self.assertFalse(hasher.verify('old-password', 'ots_bcrypt$invalid'))
        self.assertNotIn(self.raw_hash, str(hasher.safe_summary(self.raw_hash)))

    def reserve_run(self):
        run = OTSAccountSyncRun.objects.create()
        OTSAccountSyncLock.objects.filter(pk=1).update(run=run)
        return run

    def test_run_continues_after_account_error_and_releases_lock(self):
        run = self.reserve_run()
        bad = {**self.item, 'id': str(uuid.UUID(int=1)), 'passwordHash': 'invalid'}
        good = {**self.item, 'id': str(uuid.UUID(int=2))}
        page = {'items': [bad, good], 'hasMore': False, 'nextCursor': None, 'total': 2}
        with patch.object(self.service, 'fetch_page', return_value=page):
            self.service.run(run.pk)
        run.refresh_from_db()
        self.assertEqual((run.status, run.created_count, run.failed_count), ('PARTIAL', 1, 1))
        self.assertEqual(run.errors.get().code, 'INVALID_SOURCE_HASH')
        self.assertIsNone(OTSAccountSyncLock.objects.get(pk=1).run_id)

    def test_page_failure_preserves_committed_accounts_and_duplicate_delivery_noop(self):
        run = self.reserve_run()
        page = {'items': [self.item], 'hasMore': True, 'nextCursor': self.item['id'], 'total': 2}
        with patch.object(self.service, 'fetch_page', side_effect=[page, SyncError('OTS_UNAVAILABLE')]):
            self.service.run(run.pk)
        run.refresh_from_db()
        self.assertEqual((run.status, run.created_count, run.error_summary), ('PARTIAL', 1, 'OTS_UNAVAILABLE'))
        with patch.object(self.service, 'fetch_page') as fetch:
            self.service.run(run.pk)
            fetch.assert_not_called()
        self.assertTrue(OTSAccountLink.objects.exists())

    def test_empty_source_does_not_delete_or_disable_existing_accounts(self):
        self.service.sync_account(self.item)
        run = self.reserve_run()
        with patch.object(self.service, 'fetch_page', return_value={
                'items': [], 'hasMore': False, 'nextCursor': None, 'total': 0}):
            self.service.run(run.pk)
        self.assertTrue(self.synced_user().is_active)
        run.refresh_from_db()
        self.assertEqual(run.status, 'SUCCEEDED')

    def test_invalid_pagination_is_rejected_before_writes(self):
        page = {'items': [self.item], 'hasMore': True, 'nextCursor': self.item['id'], 'total': 1}
        self.service.validate_page(page, None)
        for malformed in [{**page, 'nextCursor': 'not-a-uuid'}, {**page, 'hasMore': 'yes'},
                          {**page, 'items': [self.item, self.item]}, {**page, 'total': True}]:
            with self.assertRaisesRegex(SyncError, 'SOURCE_DATA_INVALID'):
                self.service.validate_page(malformed, None)
        with self.assertRaisesRegex(SyncError, 'SOURCE_DATA_INVALID'):
            self.service.validate_page(page, self.item['id'])

    def test_queue_reserves_before_publish_and_rejects_second_run(self):
        actor = User.objects.create_superuser('admin', password='secret')
        with patch('judge.tasks.ots_account_sync.sync_ots_accounts.delay'):
            run = queue_sync(actor)
            with self.assertRaisesRegex(SyncError, 'SYNC_ALREADY_RUNNING'):
                queue_sync(actor)
        self.assertEqual(OTSAccountSyncLock.objects.get(pk=1).run_id, run.pk)

    def test_queue_failure_is_safe_and_recoverable(self):
        actor = User.objects.create_superuser('admin', password='secret')
        with patch('judge.tasks.ots_account_sync.sync_ots_accounts.delay', side_effect=RuntimeError('secret')):
            run = queue_sync(actor)
        self.assertEqual(run.status, 'FAILED')
        self.assertEqual(run.error_summary, 'QUEUE_UNAVAILABLE')
        self.assertIsNone(OTSAccountSyncLock.objects.get(pk=1).run_id)

    def test_http_auth_errors_do_not_retry_and_network_retries_are_bounded(self):
        with requests.Session() as session:
            response = requests.Response()
            response.status_code = 401
            response._content_consumed = True
            with patch.object(session, 'post', return_value=response) as post:
                with self.assertRaisesRegex(SyncError, 'INVALID_SERVICE_TOKEN'):
                    self.service.fetch_page(session, None)
                self.assertEqual(post.call_count, 1)
            with patch.object(session, 'post', side_effect=requests.Timeout('secret')) as post, \
                    patch('judge.services.ots_account_sync.time.sleep'):
                with self.assertRaisesRegex(SyncError, 'OTS_UNAVAILABLE'):
                    self.service.fetch_page(session, None)
                self.assertEqual(post.call_count, 3)

    def test_configuration_fails_closed(self):
        for url in ('http://public.example.test', 'https://user:secret@ots.test', 'https://ots.test?x=1'):
            self.service.url = url
            with self.assertRaisesRegex(SyncError, 'INVALID_CONFIGURATION'):
                self.service.validate_configuration()

    def test_recovery_requires_worker_acknowledgement_and_correct_run(self):
        run = self.reserve_run()
        with self.assertRaises(CommandError):
            call_command('recover_ots_sync', run.pk, stdout=io.StringIO())
        with self.assertRaises(CommandError):
            call_command('recover_ots_sync', run.pk + 1, worker_stopped=True, stdout=io.StringIO())
        self.assertEqual(OTSAccountSyncLock.objects.get(pk=1).run_id, run.pk)
        call_command('recover_ots_sync', run.pk, worker_stopped=True, stdout=io.StringIO())
        run.refresh_from_db()
        self.assertEqual((run.status, run.error_summary), ('FAILED', 'WORKER_INTERRUPTED'))
        self.assertIsNone(OTSAccountSyncLock.objects.get(pk=1).run_id)

    def test_admin_superuser_post_only_csrf_and_result_sanitization(self):
        actor = User.objects.create_superuser('admin', password='secret')
        Profile.objects.create(user=actor)
        client = Client(enforce_csrf_checks=True)
        client.force_login(actor)
        endpoint = reverse('admin:auth_user_sync_ots')
        self.assertEqual(client.get(endpoint).status_code, 405)
        self.assertEqual(client.post(endpoint).status_code, 403)
        page = client.get(reverse('admin:auth_user_changelist'))
        self.assertContains(page, 'Sync Up OTS')
        self.assertContains(page, 'ots-confirm')
        with patch('judge.tasks.ots_account_sync.sync_ots_accounts.delay'):
            response = client.post(endpoint, {'csrfmiddlewaretoken': client.cookies['csrftoken'].value})
        self.assertEqual(response.status_code, 302)
        result = client.get(response.url)
        self.assertContains(result, 'QUEUED')
        self.assertNotContains(result, self.raw_hash)
        staff = User.objects.create_user('staff', password='secret', is_staff=True)
        Profile.objects.create(user=staff)
        staff.user_permissions.add(Permission.objects.get(content_type__app_label='auth', codename='view_user'))
        client.force_login(staff)
        self.assertNotContains(client.get(reverse('admin:auth_user_changelist')), 'Sync Up OTS')
        self.assertEqual(client.get(response.url).status_code, 403)
        denied = client.post(endpoint, {'csrfmiddlewaretoken': client.cookies['csrftoken'].value})
        self.assertEqual(denied.status_code, 403)

    def test_admin_local_disable_applies_immediately(self):
        self.service.sync_account(self.item)
        actor = User.objects.create_superuser('admin', password='secret')
        Profile.objects.create(user=actor)
        client = Client()
        client.force_login(actor)
        link = OTSAccountLink.objects.get(user=self.synced_user())
        response = client.post(reverse('admin:judge_otsaccountlink_change', args=[link.pk]),
                               {'local_disabled': 'on', 'identity_verified': 'on', '_save': 'Save'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.synced_user().is_active)
        self.service.sync_account(self.item)
        self.assertFalse(self.synced_user().is_active)

    def test_admin_password_change_denied_for_linked_user(self):
        self.service.sync_account(self.item)
        actor = User.objects.create_superuser('admin', password='secret')
        Profile.objects.create(user=actor)
        client = Client()
        client.force_login(actor)
        url = reverse('admin:auth_user_password_change', args=[self.synced_user().pk])
        self.assertEqual(client.get(url).status_code, 403)


class SyncConcurrencyTests(TransactionTestCase):
    def test_two_requests_reserve_only_one_queued_run(self):
        OTSAccountSyncLock.objects.get_or_create(pk=1)
        actor = User.objects.create_superuser('concurrent-admin', password='secret')
        barrier = Barrier(2)

        def enqueue():
            try:
                admin_user = User.objects.get(pk=actor.pk)
                barrier.wait(timeout=10)
                try:
                    queue_sync(admin_user)
                    return 'QUEUED'
                except SyncError as error:
                    return str(error)
            finally:
                connections.close_all()

        with patch('judge.tasks.ots_account_sync.sync_ots_accounts.delay'), ThreadPoolExecutor(2) as executor:
            results = list(executor.map(lambda _: enqueue(), range(2)))
        self.assertCountEqual(results, ['QUEUED', 'SYNC_ALREADY_RUNNING'])
        self.assertEqual(OTSAccountSyncRun.objects.filter(triggered_by=actor).count(), 1)
