from functools import wraps

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError
from django.http import Http404, JsonResponse
from django.template.response import TemplateResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from judge.monitoring import judge_detail, overview


def monitoring_access(view):
    @wraps(view)
    @never_cache
    @require_GET
    def wrapped(request, *args, **kwargs):
        user = request.user
        if not (user.is_active and user.is_staff and user.has_perm('judge.view_judge') and
                user.has_perm('judge.view_all_submission')):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return wrapped


@monitoring_access
def dashboard(request, name=None):
    return TemplateResponse(request, 'admin/monitor.html', dict(
        admin.site.each_context(request), title='System monitoring', judge_name=name or ''))


def integer_parameter(request, key, maximum):
    value = request.GET.get(key, '0')
    if not value.isascii() or not value.isdigit() or len(value) > 10 or int(value) > maximum:
        raise ValueError('Invalid pagination')
    return int(value)


@monitoring_access
def overview_api(request):
    try:
        offset = integer_parameter(request, 'offset', 1000000)
    except ValueError:
        return JsonResponse({'error': 'invalid_offset'}, status=400)
    return JsonResponse(overview(offset))


@monitoring_access
def judge_api(request, name):
    try:
        after = integer_parameter(request, 'after', 2147483647)
    except ValueError:
        return JsonResponse({'error': 'invalid_after'}, status=400)
    try:
        data = judge_detail(name, after)
    except DatabaseError:
        return JsonResponse({'error': 'database_unavailable'}, status=503)
    if data is None:
        raise Http404
    return JsonResponse(data)
