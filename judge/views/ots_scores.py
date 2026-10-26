import hmac
from urllib.parse import urlparse

from django.conf import settings
from django.db.models import Sum
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_GET

from judge.models import Contest, ContestParticipation

MAX_PAGE_SIZE = 500


def contest_key_from(value):
    """Accept a bare contest key or a path/URL such as /contest/<key>/ranking/."""
    value = (value or '').strip()
    parts = [part for part in urlparse(value).path.split('/') if part]
    if 'contest' in parts and parts.index('contest') + 1 < len(parts):
        return parts[parts.index('contest') + 1]
    return parts[0] if len(parts) == 1 else ''


@require_GET
def contest_scores(request):
    """Private, token-protected export of live participation scores for one contest."""
    expected = settings.OTS_SCORE_EXPORT_TOKEN
    supplied = request.headers.get('X-Service-Token', '')
    if len(expected) < 32 or not hmac.compare_digest(expected.encode(), supplied.encode()):
        return HttpResponse(status=401)
    key = contest_key_from(request.GET.get('contest'))
    try:
        limit = min(max(int(request.GET.get('limit', 200)), 1), MAX_PAGE_SIZE)
    except ValueError:
        return JsonResponse({'code': 'INVALID_LIMIT'}, status=400)
    if not key:
        return JsonResponse({'code': 'CONTEST_REQUIRED'}, status=400)
    try:
        contest = Contest.objects.get(key=key)
    except Contest.DoesNotExist:
        return JsonResponse({'code': 'CONTEST_NOT_FOUND'}, status=404)
    after = request.GET.get('after', '')
    participations = ContestParticipation.objects.filter(
        contest=contest, virtual=ContestParticipation.LIVE, user__user__username__gt=after,
    ).select_related('user__user').order_by('user__user__username')[:limit + 1]
    rows = list(participations)
    has_more = len(rows) > limit
    items = [{
        'username': row.user.user.username,
        'score': row.score,
        'cumtime': row.cumtime,
        'isDisqualified': row.is_disqualified,
    } for row in rows[:limit]]
    max_score = contest.contest_problems.aggregate(total=Sum('points'))['total'] or 0
    response = JsonResponse({
        'contest': contest.key,
        'maxScore': max_score,
        'items': items,
        'hasMore': has_more,
        'nextCursor': items[-1]['username'] if has_more else None,
    })
    response['Cache-Control'] = 'no-store'
    return response
