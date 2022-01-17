import json
import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponseNotFound
from django.utils import timezone
from drf_yasg.utils import swagger_auto_schema
from rest_framework.decorators import api_view

from plantit.agents.models import Agent, AgentAccessPolicy
from plantit.utils import agent_to_dict, is_healthy
from plantit.redis import RedisClient

logger = logging.getLogger(__name__)


@swagger_auto_schema(methods='get')
@login_required
@api_view(['GET'])
def list(request):
    # only return public agents and agents the requesting user is authorized to access
    agents = [agent for agent in Agent.objects.all() if agent.public or request.user.username in [u.username for u in agent.users_authorized.all()]]
    return JsonResponse({'agents': [agent_to_dict(agent, request.user) for agent in agents]})


@swagger_auto_schema(methods='get')
@login_required
@api_view(['GET'])
def get(request, name):
    try:
        agent = Agent.objects.get(name=name)

        # if the requesting user doesn't own the agent and isn't on its
        # list of authorized users, they're not authorized to access it
        if not agent.public and request.user.username not in [u.username for u in agent.users_authorized.all()]: return HttpResponseNotFound()
    except: return HttpResponseNotFound()
    return JsonResponse(agent_to_dict(agent, request.user))


@swagger_auto_schema(methods='get')
@login_required
@api_view(['GET'])
def exists(request, name):
    try:
        agent = Agent.objects.get(name=name)

        # if the requesting user doesn't own the agent and isn't on its
        # list of authorized users, they're not authorized to access it
        if not agent.public and request.user.username not in [u.username for u in agent.users_authorized.all()]: return JsonResponse({'exists': False})
        return JsonResponse({'exists': True})
    except: return JsonResponse({'exists': False})


@swagger_auto_schema(methods='post')
@login_required
@api_view(['POST'])
def healthcheck(request, name):
    try:
        agent = Agent.objects.get(name=name)

        # if the requesting user doesn't own the agent and isn't on its
        # list of authorized users, they're not authorized to access it
        if not agent.public and request.user.username not in [u.username for u in agent.users_authorized.all()]: return HttpResponseNotFound()
    except: return HttpResponseNotFound()

    body = json.loads(request.body.decode('utf-8'))
    healthy, output = is_healthy(agent, body['auth'])
    check = {
        'timestamp': timezone.now().isoformat(),
        'healthy': healthy,
        'output': output
    }

    # persist health status to DB
    agent.is_healthy = healthy
    agent.save()

    # update cache
    redis = RedisClient.get()
    length = redis.llen(f"healthchecks/{agent.name}")
    checks_saved = int(settings.AGENTS_HEALTHCHECKS_SAVED)
    if length > checks_saved: redis.rpop(f"healthchecks/{agent.name}")
    redis.lpush(f"healthchecks/{agent.name}", json.dumps(check))
    return JsonResponse(check)


@swagger_auto_schema(methods='get')
@login_required
@api_view(['GET'])
def healthchecks(request, name):
    try:
        agent = Agent.objects.get(name=name)

        # if the requesting user doesn't own the agent and isn't on its
        # list of authorized users, they're not authorized to access it
        if not agent.public and request.user.username not in [u.username for u in agent.users_authorized.all()]: return HttpResponseNotFound()
    except: return HttpResponseNotFound()

    redis = RedisClient.get()
    checks = [json.loads(check) for check in redis.lrange(f"healthchecks/{agent.name}", 0, -1)]
    return JsonResponse({'healthchecks': checks})


@swagger_auto_schema(methods='get')
@login_required
@api_view(['GET'])
def policies(request, name):
    try:
        agent = Agent.objects.get(name=name)

        # if the requesting user doesn't own the agent and isn't on its
        # list of authorized users, they're not authorized to access it
        if not agent.public and request.user.username not in [u.username for u in agent.users_authorized.all()]: return HttpResponseNotFound()
    except: return HttpResponseNotFound()
    return JsonResponse({'policies': list(AgentAccessPolicy.objects.filter(agent=agent))})
