import json

from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import JsonResponse, HttpResponseNotFound
from django.utils.decorators import method_decorator
from drf_yasg.utils import swagger_auto_schema
from rest_framework.decorators import api_view

from plantit.redis import RedisClient
from plantit.utils import get_institutions, get_total_counts, get_aggregate_timeseries, get_workflow_usage_timeseries, get_user_timeseries


@swagger_auto_schema(methods='get')
@login_required
@api_view(['get'])
def institutions_info(_):
    redis = RedisClient.get()
    cached = list(redis.scan_iter(match=f"institutions/*"))

    if len(cached) != 0:
        institutions = [json.loads(redis.get(key)) for key in cached]
    else:
        institutions = get_institutions()
        for i in institutions: redis.set(f"institutions/{i['name']}", json.dumps(i))

    return JsonResponse({'institutions': institutions})


@swagger_auto_schema(methods='get')
@login_required
@api_view(['get'])
def aggregate_counts(_):
    redis = RedisClient.get()
    cached = redis.get("stats_counts")

    if cached is not None:
        counts = json.loads(cached)
    else:
        counts = get_total_counts()
        redis.set("stats_counts", json.dumps(counts))

    return JsonResponse(counts)


@swagger_auto_schema(methods='get')
@login_required
@api_view(['get'])
def aggregate_timeseries(_):
    redis = RedisClient.get()
    cached = redis.get("total_timeseries")

    if cached is not None:
        series = json.loads(cached)
    else:
        series = get_aggregate_timeseries()
        redis.set("total_timeseries", json.dumps(series))

    return JsonResponse(series)


@login_required
def workflow_timeseries(_, owner, name, branch):
    redis = RedisClient.get()
    cached = redis.get(f"workflow_timeseries/{owner}/{name}/{branch}")

    if cached is not None:
        series = json.loads(cached)
    else:
        series = get_workflow_usage_timeseries(owner, name, branch)
        redis.set(f"workflow_timeseries/{owner}/{name}/{branch}", json.dumps(series))

    return JsonResponse(series)


@login_required
def user_timeseries(request, username):
    try: user = User.objects.get(username=username)
    except: return HttpResponseNotFound()

    redis = RedisClient.get()
    cached = redis.get(f"user_timeseries/{user.username}")

    if cached is not None:
        series = json.loads(cached)
    else:
        series = get_user_timeseries(user.username)
        redis.set(f"user_timeseries/{user.username}", json.dumps(series))

    return JsonResponse(series)
