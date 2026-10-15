import json
import socket
import struct
import threading
import time
import zlib
from socketserver import TCPServer
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import resolve, reverse

from judge.bridge.django_handler import DjangoHandler
from judge.bridge.judge_list import JudgeList
from judge.models import Judge, Language, Profile, Submission, SubmissionTestCase
from judge.models.tests.util import create_problem
from judge.monitor_metrics import clean_metrics
from judge.monitoring import bridge_snapshot, cached_read, cached_write, overview
from judge.views.monitor import dashboard, judge_api, overview_api


class FakeJudge(SimpleNamespace):
    __hash__ = object.__hash__


class MonitoringTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def request(self, path='/', method='get', permitted=True):
        request = getattr(self.factory, method)(path)
        request.user = SimpleNamespace(is_active=True, is_staff=True, has_perm=lambda permission: permitted)
        return request

    def judge(self, **kwargs):
        values = dict(name='worker', tier=1, is_disabled=False, working=False, latency=0.1, load=0.2,
                      problems={'sum'}, executors={'PY3': []}, get_current_submission=lambda: None)
        values.update(kwargs)
        return FakeJudge(**values)

    def scheduler(self):
        scheduler = JudgeList()
        scheduler.min_tier = 1
        scheduler.judges.add(self.judge())
        return scheduler

    def enqueue(self, scheduler, sid=42, priority=0, language='PY3', target=None, banned=()):
        scheduler.node_map[sid] = scheduler.queue.insert(
            (sid, 'sum', language, 'SECRET SOURCE', target, banned), scheduler.priority[priority],
        )

    def test_snapshot_real_queue_order_and_redaction(self):
        scheduler = self.scheduler()
        self.enqueue(scheduler, sid=1, priority=2)
        self.enqueue(scheduler, sid=2, priority=0)
        state = scheduler.monitoring_snapshot()
        self.assertEqual([item['id'] for item in state['queue']], [2, 1])
        self.assertEqual(state['queue_total'], 2)
        self.assertNotIn('SECRET', json.dumps(state))
        self.assertEqual(scheduler.monitoring_snapshot(1, 1)['queue'][0]['id'], 1)
        self.assertEqual(len(scheduler.node_map), 2)

    def test_diagnostics_constraints(self):
        for changes, enqueue, expected in [
            ({'working': True}, {}, 'busy'),
            ({'is_disabled': True}, {}, 'disabled'),
            ({'tier': 2}, {}, 'standby tier'),
            ({}, {'banned': ['worker']}, 'contest ban'),
            ({}, {'target': 'other'}, 'assigned to another judge'),
            ({'problems': set()}, {}, 'problem unavailable'),
            ({}, {'language': 'CPP'}, 'runtime unavailable'),
        ]:
            with self.subTest(expected=expected):
                scheduler = self.scheduler()
                scheduler.judges = {self.judge(**changes)}
                self.enqueue(scheduler, **enqueue)
                self.assertEqual(scheduler.monitoring_snapshot()['judges'][0]['diagnostics'][0]['reason'], expected)

    def test_reservation_and_assignment(self):
        scheduler = self.scheduler()
        scheduler.judges.add(self.judge(name='busy', working=True, get_current_submission=lambda: 99))
        self.enqueue(scheduler, priority=3)
        state = scheduler.monitoring_snapshot()
        idle = next(item for item in state['judges'] if item['name'] == 'worker')
        self.assertEqual(idle['diagnostics'][0]['reason'], 'reserved for higher priority work')
        self.assertEqual(next(item for item in state['judges'] if item['name'] == 'busy')['submission'], 99)

    def test_snapshot_waits_for_scheduler_lock(self):
        scheduler = self.scheduler()
        entered, finished = threading.Event(), threading.Event()

        def take_snapshot():
            entered.set()
            scheduler.monitoring_snapshot()
            finished.set()

        with scheduler.lock:
            worker = threading.Thread(target=take_snapshot)
            worker.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(finished.wait(0.02))
        worker.join(1)
        self.assertTrue(finished.is_set())

    def test_large_queue_snapshot_is_bounded(self):
        scheduler = self.scheduler()
        for sid in range(10000):
            self.enqueue(scheduler, sid=sid)
        state = scheduler.monitoring_snapshot()
        self.assertEqual(len(state['queue']), 100)
        self.assertEqual(state['queue_total'], 10000)
        self.assertEqual(len(state['judges'][0]['diagnostics']), 100)

    @override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.dummy.DummyCache'}})
    @patch('judge.monitoring.Judge')
    @patch('judge.monitoring.bridge_snapshot')
    def test_database_failure_preserves_live_assignments(self, bridge, judges):
        scheduler = self.scheduler()
        activity = {'submission': 42, 'started': time.time() - 200, 'updated': time.time() - 150}
        scheduler.judges = {self.judge(working=True, get_current_submission=lambda: 42, monitor_activity=activity)}
        bridge.return_value = scheduler.monitoring_snapshot()
        judges.objects.values.side_effect = DatabaseError('offline')
        data = overview()
        self.assertFalse(data['database']['available'])
        self.assertEqual(data['judges'][0]['submission'], 42)
        self.assertIsNone(data['judges'][0]['received_testcases'])
        self.assertTrue(any('no grading progress' in item for item in data['alerts']))

    def test_snapshot_packet_validation(self):
        handler = object.__new__(DjangoHandler)
        handler.judges = self.scheduler()
        for value in (-1, True, '1', 1000001):
            self.assertEqual(handler.on_monitor_state({'offset': value})['name'], 'bad-request')
        self.assertEqual(handler.on_monitor_state({'limit': 101})['name'], 'bad-request')
        self.assertEqual(handler.on_monitor_state({})['name'], 'monitor-state')

    def test_bridge_protocol_round_trip(self):
        scheduler = self.scheduler()
        self.enqueue(scheduler)

        def handle(request, address, server):
            DjangoHandler(request, address, server, scheduler)

        with TCPServer(('127.0.0.1', 0), handle) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
            thread.start()
            try:
                with override_settings(BRIDGED_DJANGO_CONNECT=server.server_address):
                    snapshot = bridge_snapshot()
                self.assertEqual(snapshot['queue'][0]['id'], 42)
                self.assertEqual(snapshot['queue_total'], 1)
                self.assertEqual(len(scheduler.node_map), 1)
            finally:
                server.shutdown()
                thread.join(1)

    def test_metrics_allowlist(self):
        self.assertEqual(clean_metrics({'cpu_percent': float('nan'), 'cpu_count': True, 'auth_key': 'secret',
                                        'disk_free_bytes': -1, 'network_sent_bytes': 20}),
                         {'network_sent_bytes': 20})
        self.assertEqual(clean_metrics(None), {})
        self.assertEqual(clean_metrics({'cpu_count': 2 ** 10000}), {})

    def test_cache_failure_is_optional(self):
        with patch('judge.monitoring.cache.get', side_effect=OSError('offline')):
            self.assertIsNone(cached_read('test'))
        with patch('judge.monitoring.cache.set', side_effect=OSError('offline')):
            cached_write('test', {}, 3)

    @override_settings(STATICFILES_STORAGE='django.contrib.staticfiles.storage.StaticFilesStorage')
    def test_routes_and_admin_template_render(self):
        self.assertEqual(resolve('/admin/monitor/').func, dashboard)
        self.assertEqual(reverse('admin_monitor_overview'), '/admin/monitor/api/overview/')
        request = self.factory.get('/admin/monitor/')
        request.user = User(username='preview', is_active=True, is_staff=True, is_superuser=True)
        with patch('judge.views.monitor.admin.site.each_context', return_value={'available_apps': []}):
            response = dashboard(request)
            response.render()
        self.assertContains(response, 'Bridge queue (scheduler order)')
        self.assertContains(response, 'monitor.js')

    def test_explicit_authorization_on_all_views(self):
        for view, kwargs in [(dashboard, {}), (overview_api, {}), (judge_api, {'name': 'worker'})]:
            with self.subTest(view=view.__name__):
                with self.assertRaises(PermissionDenied):
                    view(self.request(permitted=False), **kwargs)
                request = self.request()
                request.user = AnonymousUser()
                with self.assertRaises(PermissionDenied):
                    view(request, **kwargs)
                self.assertEqual(view(self.request(method='post'), **kwargs).status_code, 405)

    def test_invalid_pagination_rejected(self):
        for query in ('offset=-1', 'offset=abc', 'offset=1000001', 'offset=' + '1' * 100):
            self.assertEqual(overview_api(self.request('/?' + query)).status_code, 400)
        self.assertEqual(judge_api(self.request('/?after=-1'), 'worker').status_code, 400)

    @patch('judge.views.monitor.overview', return_value={'health': 'unknown'})
    def test_api_never_cached(self, mocked):
        response = overview_api(self.request())
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])

    @override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.dummy.DummyCache'}})
    @patch('judge.monitoring.Judge')
    @patch('judge.monitoring.SubmissionTestCase')
    @patch('judge.monitoring.Submission')
    @patch('judge.monitoring.history', return_value={})
    @patch('judge.monitoring.bridge_snapshot', side_effect=OSError('unreachable'))
    def test_bridge_failure_is_unknown(self, bridge, history, submission, cases, judges):
        judges.objects.values.return_value = [{'name': 'worker'}]
        submission.objects.filter.return_value.values.return_value = []
        cases.objects.filter.return_value.values.return_value.annotate.return_value = []
        data = overview()
        self.assertEqual(data['health'], 'unknown')
        self.assertIsNone(data['queue'])
        self.assertEqual(data['judges'][0]['state'], 'unknown')

    @override_settings(BRIDGED_DJANGO_CONNECT=('localhost', 9999))
    def test_socket_partial_reads_and_cleanup(self):
        client, server = socket.socketpair()
        payload = zlib.compress(json.dumps({'name': 'monitor-state', 'queue': []}).encode())
        wire = struct.pack('!I', len(payload)) + payload
        for byte in wire:
            server.sendall(bytes([byte]))
        with patch('judge.monitoring.socket.create_connection', return_value=client):
            self.assertEqual(bridge_snapshot()['queue'], [])
        self.assertEqual(client.fileno(), -1)
        server.close()

    @override_settings(BRIDGED_DJANGO_CONNECT=('localhost', 9999))
    def test_oversized_socket_response_rejected(self):
        client, server = socket.socketpair()
        server.sendall(struct.pack('!I', 8 * 1024 * 1024))
        try:
            with patch('judge.monitoring.socket.create_connection', return_value=client):
                with self.assertRaises(ValueError):
                    bridge_snapshot()
            self.assertEqual(client.fileno(), -1)
        finally:
            server.close()


@override_settings(STATICFILES_STORAGE='django.contrib.staticfiles.storage.StaticFilesStorage')
class MonitoringIntegrationTests(TestCase):
    def test_anonymous_http_access_is_forbidden(self):
        client = Client()
        for url in ('/admin/monitor/', '/admin/monitor/api/overview/',
                    '/admin/monitor/api/judges/worker/', '/admin/monitor/judges/worker/'):
            with self.subTest(url=url):
                self.assertEqual(client.get(url).status_code, 403)

    def test_staff_requires_both_permissions(self):
        user = User.objects.create_user(username='monitor-staff', is_staff=True)
        Profile.objects.create(user=user)
        self.client.force_login(user)
        self.assertEqual(self.client.get('/admin/monitor/api/overview/').status_code, 403)

    @patch('judge.views.monitor.overview', return_value={'health': 'unknown'})
    def test_superuser_api_and_page(self, mocked):
        user = User.objects.create_user(username='monitor-admin', is_superuser=True, is_staff=True)
        Profile.objects.create(user=user)
        self.client.force_login(user)
        self.assertEqual(self.client.get('/admin/monitor/api/overview/').json()['health'], 'unknown')
        self.assertContains(self.client.get('/admin/monitor/'), 'Bridge queue (scheduler order)')
        self.assertEqual(self.client.post('/admin/monitor/api/overview/').status_code, 405)

    @override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.dummy.DummyCache'}})
    @patch('judge.monitoring.bridge_snapshot')
    def test_live_assignment_and_testcases_override_historical_assignment(self, bridge):
        user = User.objects.create_user(username='monitor-detail-admin', is_superuser=True, is_staff=True)
        profile = Profile.objects.create(user=user)
        old = Judge.objects.create(name='old-worker', auth_key='never-expose-old-key')
        current = Judge.objects.create(name='current-worker', auth_key='never-expose-current-key')
        submission = Submission.objects.create(user=profile, problem=create_problem(code='monitor-sum'),
                                               language=Language.get_python3(), judged_on=old, status='G',
                                               current_testcase=102)
        SubmissionTestCase.objects.bulk_create([
            SubmissionTestCase(submission=submission, case=case, status='AC', output='never-expose-output')
            for case in range(1, 102)
        ])
        bridge.return_value = {
            'name': 'monitor-state', 'captured_at': time.time(), 'queue': [], 'queue_total': 0,
            'offset': 0, 'limit': 100, 'min_tier': 1,
            'judges': [{'name': current.name, 'submission': submission.id, 'disabled': False,
                        'activity': {'submission': submission.id, 'updated': time.time()}, 'metrics': {}}],
        }
        self.client.force_login(user)
        response = self.client.get('/admin/monitor/api/judges/current-worker/')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['judge']['submission'], submission.id)
        self.assertEqual(data['judge']['received_testcases'], 101)
        self.assertIsNone(data['judge']['total_testcases'])
        self.assertEqual(len(data['testcases']), 100)
        self.assertTrue(data['has_more'])
        next_page = self.client.get('/admin/monitor/api/judges/current-worker/?after=100').json()
        self.assertEqual(next_page['testcases'][0]['case'], 101)
        self.assertFalse(next_page['has_more'])
        self.assertNotIn('never-expose', response.content.decode())
