import ipaddress
import json
import random
import re
import time
import uuid
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import DatabaseError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from judge.auth.ots_password_hasher import valid_source_hash
from judge.models import OTSAccountLink, OTSAccountSyncError, OTSAccountSyncLock, OTSAccountSyncRun, Profile


class SyncError(Exception):
    """Only stable, non-sensitive error codes may cross this boundary."""


STUDENT_ID = re.compile(r'[BN][0-9]{2}[A-Z]{4}[0-9]{3}', re.ASCII)


def queue_sync(actor):
    if not actor.is_active or not actor.is_superuser:
        raise SyncError('PERMISSION_DENIED')
    with transaction.atomic():
        lock = OTSAccountSyncLock.objects.select_for_update().get(pk=1)
        if lock.run_id is not None:
            raise SyncError('SYNC_ALREADY_RUNNING')
        run = OTSAccountSyncRun.objects.create(triggered_by=actor)
        lock.run = run
        lock.save(update_fields=['run'])
    from judge.tasks.ots_account_sync import sync_ots_accounts
    try:
        sync_ots_accounts.delay(run.pk)
    except Exception:
        # A broker timeout may still have delivered the message. Fail only if
        # the worker has not claimed it; a late delivery then becomes a no-op.
        with transaction.atomic():
            lock = OTSAccountSyncLock.objects.select_for_update().get(pk=1)
            run.refresh_from_db()
            if lock.run_id == run.pk and run.status == 'QUEUED':
                run.status = 'FAILED'
                run.error_summary = 'QUEUE_UNAVAILABLE'
                run.finished_at = timezone.now()
                run.save()
                lock.run = None
                lock.save(update_fields=['run'])
    return run


def validate_account(item):
    if not isinstance(item, dict):
        raise SyncError('SOURCE_DATA_INVALID')
    try:
        source_id = uuid.UUID(item['id'])
        student = item['studentId'].strip().upper()
        name, email = item['fullName'], item['email']
        updated = parse_datetime(item['updatedAt'])
        if not STUDENT_ID.fullmatch(student):
            raise SyncError('INVALID_STUDENT_ID')
        if (item['role'] != 'USER' or item['status'] not in ('ACTIVE', 'BLOCKED') or
                not isinstance(name, str) or not name.strip() or len(name) > 150 or
                not isinstance(email, str) or len(email) > 254 or
                updated is None or timezone.is_naive(updated)):
            raise SyncError('SOURCE_DATA_INVALID')
        validate_email(email)
        if not valid_source_hash(item.get('passwordHash')):
            raise SyncError('INVALID_SOURCE_HASH')
        return source_id, student, name, email, updated
    except (KeyError, TypeError, ValueError, AttributeError, ValidationError):
        raise SyncError('SOURCE_DATA_INVALID') from None


class OTSAccountSyncService:
    def __init__(self):
        self.url = settings.OTS_SYNC_API_URL.rstrip('/')
        self.token = settings.OTS_SYNC_SERVICE_TOKEN
        self.timeout = settings.OTS_SYNC_TIMEOUT_SECONDS
        self.page_size = settings.OTS_SYNC_PAGE_SIZE

    def validate_configuration(self):
        parsed = urlsplit(self.url)
        internal = False
        try:
            address = ipaddress.ip_address(parsed.hostname or '')
            internal = address.is_loopback or address in ipaddress.ip_network('10.0.0.0/8') or \
                address in ipaddress.ip_network('172.16.0.0/12') or address in ipaddress.ip_network('192.168.0.0/16')
        except ValueError:
            # HTTP by hostname requires an explicit deployment assertion that
            # the network is private/protected; HTTPS is the default.
            internal = settings.OTS_SYNC_ALLOW_INTERNAL_HTTP
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or
                parsed.path not in ('', '/') or
                (parsed.scheme != 'https' and not (parsed.scheme == 'http' and internal)) or
                not isinstance(self.token, str) or len(self.token) < 32 or any(c.isspace() for c in self.token) or
                not 0 < self.timeout <= 120 or not 1 <= self.page_size <= 200):
            raise SyncError('INVALID_CONFIGURATION')

    def fetch_page(self, session, cursor):
        for attempt in range(3):
            try:
                with session.post(self.url + '/internal/oj/users/export',
                                  headers={'X-Service-Token': self.token},
                                  json={'cursor': cursor, 'limit': self.page_size},
                                  timeout=(self.timeout, self.timeout), allow_redirects=False,
                                  stream=True) as response:
                    if response.status_code in (401, 403):
                        raise SyncError('INVALID_SERVICE_TOKEN')
                    if response.status_code == 429 or response.status_code >= 500:
                        raise requests.ConnectionError()
                    if response.status_code != 200:
                        raise SyncError('SOURCE_DATA_INVALID')
                    # Bound decompressed JSON before parsing, never persist it.
                    content = bytearray()
                    for chunk in response.iter_content(65536):
                        content.extend(chunk)
                        if len(content) > 1024 * 1024:
                            raise SyncError('SOURCE_DATA_INVALID')
                    try:
                        return json.loads(content)
                    except (ValueError, UnicodeError):
                        raise SyncError('SOURCE_DATA_INVALID') from None
            except requests.RequestException:
                if attempt == 2:
                    raise SyncError('OTS_UNAVAILABLE') from None
                time.sleep(0.25 * (2 ** attempt) + random.uniform(0, 0.1))

    def validate_page(self, page, cursor):
        if (not isinstance(page, dict) or not isinstance(page.get('items'), list) or
                len(page['items']) > self.page_size or type(page.get('hasMore')) is not bool or
                type(page.get('total')) is not int or page['total'] < 0 or 'nextCursor' not in page):
            raise SyncError('SOURCE_DATA_INVALID')
        previous = uuid.UUID(cursor).int if cursor else -1
        for item in page['items']:
            try:
                current = uuid.UUID(item['id']).int
            except (ValueError, TypeError, KeyError, AttributeError):
                raise SyncError('SOURCE_DATA_INVALID') from None
            if current <= previous:
                raise SyncError('SOURCE_DATA_INVALID')
            previous = current
        if page['hasMore']:
            if (not page['items'] or page['nextCursor'] != page['items'][-1]['id']):
                raise SyncError('SOURCE_DATA_INVALID')
        elif page['nextCursor'] is not None:
            raise SyncError('SOURCE_DATA_INVALID')

    def sync_account(self, item):
        source_id, student, name, email, updated = validate_account(item)
        User = get_user_model()
        with transaction.atomic():
            link = OTSAccountLink.objects.select_for_update().filter(ots_user_id=source_id).first()
            user = User.objects.select_for_update().get(pk=link.user_id) if link else User()
            others = User.objects.exclude(pk=user.pk) if user.pk else User.objects.all()
            if others.filter(username__iexact=student).exists():
                raise SyncError('USERNAME_CONFLICT')
            if others.filter(email__iexact=email).exists():
                raise SyncError('EMAIL_CONFLICT')
            values = {'username': student, 'first_name': name, 'last_name': '', 'email': email,
                      'password': 'ots_bcrypt$' + item['passwordHash'],
                      'is_active': item['status'] == 'ACTIVE' and not (link and link.local_disabled)}
            changed = any(getattr(user, key) != value for key, value in values.items())
            for key, value in values.items():
                setattr(user, key, value)
            if not link:
                user.is_staff = user.is_superuser = False
            user.save()
            try:
                _, profile_created = Profile.objects.get_or_create(user=user)
            except Exception:
                raise SyncError('PROFILE_CREATE_FAILED') from None
            created = link is None
            if created:
                link = OTSAccountLink(user=user, ots_user_id=source_id)
            changed = changed or profile_created or link.ots_status != item['status'] or \
                link.ots_student_id != student or link.source_updated_at != updated
            link.ots_student_id = student
            link.ots_status = item['status']
            link.source_updated_at = updated
            link.last_synced_at = timezone.now()
            link.save()
            return 'created_count' if created else 'updated_count' if changed else 'unchanged_count'

    def run(self, run_id):
        with transaction.atomic():
            lock = OTSAccountSyncLock.objects.select_for_update().get(pk=1)
            run = OTSAccountSyncRun.objects.get(pk=run_id)
            if lock.run_id != run.pk or run.status != 'QUEUED':
                return
            run.status, run.started_at = 'RUNNING', timezone.now()
            run.save(update_fields=['status', 'started_at'])
        try:
            self.validate_configuration()
            cursor = None
            # Session is private to this job: no browser cookies or HTTP proxies.
            with requests.Session() as session:
                session.trust_env = False
                for _ in range(10000):
                    page = self.fetch_page(session, cursor)
                    self.validate_page(page, cursor)
                    run.total = page['total']
                    for item in page['items']:
                        try:
                            counter = self.sync_account(item)
                            setattr(run, counter, getattr(run, counter) + 1)
                        except (SyncError, DatabaseError) as error:
                            run.failed_count += 1
                            student = item.get('studentId', '')
                            student = student.strip().upper() if isinstance(student, str) else ''
                            OTSAccountSyncError.objects.create(
                                run=run, student_id=student if STUDENT_ID.fullmatch(student) else '',
                                code=str(error) if isinstance(error, SyncError) else 'DATABASE_ERROR')
                    cursor = page['nextCursor']
                    run.cursor = cursor
                    run.save()
                    if not page['hasMore']:
                        break
                else:
                    raise SyncError('SOURCE_DATA_INVALID')
            run.status = 'PARTIAL' if run.failed_count else 'SUCCEEDED'
        except Exception as error:
            run.error_summary = str(error) if isinstance(error, SyncError) else 'INTERNAL_ERROR'
            processed = run.created_count + run.updated_count + run.unchanged_count + run.failed_count
            run.status = 'PARTIAL' if processed else 'FAILED'
        finally:
            with transaction.atomic():
                lock = OTSAccountSyncLock.objects.select_for_update().get(pk=1)
                run.finished_at = timezone.now()
                run.save()
                if lock.run_id == run.pk:
                    lock.run = None
                    lock.save(update_fields=['run'])
