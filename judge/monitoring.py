"""Read-only monitoring. Bridge is authoritative for assignments and queue order."""
import json
import logging
import socket
import struct
import time
import zlib
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError
from django.db.models import Count
from django.utils import timezone

from judge.models import Judge, Submission, SubmissionTestCase

logger = logging.getLogger(__name__)
MAX_RESPONSE = 4 * 1024 * 1024


def cached_read(key):
    try:
        return cache.get(key)
    except Exception:
        logger.warning('Monitoring cache read unavailable')
        return None


def cached_write(key, value, timeout):
    try:
        cache.set(key, value, timeout)
    except Exception:
        logger.warning('Monitoring cache write unavailable')


def bridge_snapshot(offset=0):
    # A separate client keeps the grading command contract unchanged.
    deadline = time.monotonic() + 2
    address = settings.BRIDGED_DJANGO_CONNECT or settings.BRIDGED_DJANGO_ADDRESS[0]
    with socket.create_connection(address, timeout=2) as sock:
        def read_exact(size):
            chunks = []
            while size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Bridge monitoring deadline')
                sock.settimeout(remaining)
                chunk = sock.recv(size)
                if not chunk:
                    raise ValueError('Incomplete Bridge response')
                chunks.append(chunk)
                size -= len(chunk)
            return b''.join(chunks)

        payload = zlib.compress(json.dumps({'name': 'monitor-state', 'offset': offset, 'limit': 100}).encode())
        sock.sendall(struct.pack('!I', len(payload)) + payload)
        length, = struct.unpack('!I', read_exact(4))
        if not 0 < length <= MAX_RESPONSE:
            raise ValueError('Oversized Bridge response')
        decoder = zlib.decompressobj()
        raw = decoder.decompress(read_exact(length), MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE or not decoder.eof:
            raise ValueError('Invalid Bridge response')
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get('name') != 'monitor-state':
            raise ValueError('Unsupported Bridge response')
        return data


def history():
    """Indexed submission arrival cohort; never presented as completion throughput."""
    key = 'monitor:history:v1'
    result = cached_read(key)
    if result is None:
        now = timezone.now()
        rows = list(Submission.objects.filter(date__gte=now - timedelta(hours=1))
                    .values('status', 'result').annotate(count=Count('id')).order_by())
        total = sum(row['count'] for row in rows)
        errors = sum(row['count'] for row in rows if row['status'] == 'IE' or row['result'] == 'IE')
        result = {'window_seconds': 3600, 'cohort': 'submissions received during the last hour',
                  'sampled_at': now.isoformat(), 'received': total, 'internal_errors': errors,
                  'error_rate': errors / total if total else None, 'states': rows,
                  'completion_throughput_per_minute': None, 'end_to_end_latency_seconds': None}
        cached_write(key, result, 30)
    return result


def overview(offset=0):
    result = {'bridge': {'available': False}, 'database': {'available': False},
              'judges': [], 'queue': None, 'queue_total': None, 'history': None, 'alerts': [],
              'unassigned_in_progress': None}
    try:
        # Shared cache reduces duplicate polling across administrators.
        key = 'monitor:bridge:v1:%d' % offset
        state = cached_read(key)
        if state is None:
            state = bridge_snapshot(offset)
            cached_write(key, state, 3)
        result.update(queue=state['queue'], queue_total=state['queue_total'],
                      offset=state['offset'], limit=state['limit'], min_tier=state['min_tier'])
        result['bridge'] = {'available': True, 'captured_at': state['captured_at']}
        execution = dict(state.get('execution', {}))
        durations = execution.pop('grading_seconds', [])
        window = execution.get('window_seconds', 0)
        completed = execution.get('completed', 0)
        execution['throughput_per_minute'] = completed * 60 / window if window > 0 else None
        execution['mean_dispatch_to_finish_seconds'] = sum(durations) / len(durations) if durations else None
        execution['internal_error_rate'] = execution.get('internal_errors', 0) / completed if completed else None
        result['execution'] = execution
        live = {judge['name']: judge for judge in state['judges']}
    except (OSError, ValueError, KeyError, TypeError):
        live = {}
        result['alerts'].append('Bridge unavailable or incompatible; live state is unknown.')

    try:
        judges = list(Judge.objects.values('id', 'name', 'is_disabled', 'is_blocked', 'tier'))
        active_ids = [item['submission'] for item in live.values() if item['submission']]
        submissions = {item['id']: item for item in Submission.objects.filter(id__in=active_ids).values(
            'id', 'status', 'result', 'problem__code', 'language__key', 'current_testcase', 'judged_date')}
        counts = {item['submission_id']: item['count'] for item in SubmissionTestCase.objects.filter(
            submission_id__in=active_ids).values('submission_id').annotate(count=Count('id'))}
        result['history'] = history()
        if result['bridge']['available']:
            result['unassigned_in_progress'] = list(Submission.objects.filter(status__in=('P', 'G'))
                                                    .exclude(id__in=active_ids).order_by('-id')
                                                    .values('id', 'status', 'judged_date')[:20])
        result['database'] = {'available': True}
    except DatabaseError:
        logger.exception('Monitoring database query failed')
        judges = [{'name': name} for name in live]
        submissions, counts = {}, {}
        result['alerts'].append('Database unavailable; submission details and history are unknown.')

    now = time.time()
    for record in judges:
        item = live.get(record['name'])
        if item is None:
            record['state'] = 'offline' if result['bridge']['available'] else 'unknown'
        else:
            record.update(item)
            record['state'] = 'busy' if item['submission'] else ('disabled' if item['disabled'] else 'idle')
            activity = item.get('activity', {})
            # Older activity may belong to a previous grading run.
            if activity.get('submission') != item['submission']:
                activity = {}
            record['activity'] = activity
            record['submission_detail'] = submissions.get(item['submission'])
            record['received_testcases'] = (counts.get(item['submission'], 0)
                                            if result['database']['available'] and item['submission'] else None)
            record['total_testcases'] = None  # Short circuit, generators and batches prevent inferring a total.
            if item['submission'] and activity.get('updated') and now - activity['updated'] > 120:
                result['alerts'].append('%s: no grading progress for over 120 seconds; investigate.' % item['name'])
            if item.get('last_ping') and now - item['last_ping'] > 30:
                result['alerts'].append('%s: heartbeat older than 30 seconds.' % item['name'])
        result['judges'].append(record)
    result['health'] = ('unknown' if not result['bridge']['available'] or not result['database']['available']
                        else 'degraded' if result['alerts'] or not any(
                            item['state'] in ('idle', 'busy') and not item.get('disabled')
                            for item in result['judges']) else 'healthy')
    return result


def judge_detail(name, after=0):
    data = overview()
    judge = next((item for item in data['judges'] if item['name'] == name), None)
    if judge is None:
        return None
    cases = None
    submission = judge.get('submission')
    if submission and data['database']['available']:
        cases = list(SubmissionTestCase.objects.filter(submission_id=submission, case__gt=after)
                     .order_by('case').values('case', 'status', 'time', 'memory', 'points', 'total', 'batch')[:101])
    return {'judge': judge, 'testcases': cases[:100] if cases is not None else None,
            'has_more': len(cases) > 100 if cases is not None else False,
            'bridge': data['bridge'], 'database': data['database']}
