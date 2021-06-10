import json
import logging

from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponseNotFound, HttpResponseForbidden

from plantit.github import get_repo_readme, get_repo
from plantit.redis import RedisClient
from plantit.users.models import Profile
from plantit.users.utils import get_django_profile
from plantit.workflows.models import Workflow
from plantit.workflows.utils import bind_workflow_bundle, get_personal_workflow_bundles, get_public_workflow_bundles, get_workflow_bundle

logger = logging.getLogger(__name__)

# TODO: when this (https://code.djangoproject.com/ticket/31949) gets merged, remove the sync_to_async/async_to_sync hack


@sync_to_async
@login_required
@async_to_sync
async def list_public(request):
    invalidate = request.GET.get('invalidate', False)
    bundles = await get_public_workflow_bundles(invalidate=bool(invalidate))
    return JsonResponse({'workflows': bundles})


@sync_to_async
@login_required
@async_to_sync
async def list_personal(request, owner):
    profile = await get_django_profile(request.user)
    if owner != profile.github_username:
        try:
            await sync_to_async(Profile.objects.get)(github_username=owner)
        except:
            return HttpResponseNotFound()

    invalidate = request.GET.get('invalidate', False)
    bundles = await get_personal_workflow_bundles(owner=owner, invalidate=bool(invalidate))
    return JsonResponse({'workflows': bundles})


@sync_to_async
@login_required
@async_to_sync
async def get(request, owner, name):
    invalidate = request.GET.get('invalidate', False)
    bundle = await get_workflow_bundle(owner=owner, name=name, token=request.user.profile.github_token, invalidate=bool(invalidate))
    return HttpResponseNotFound() if bundle is None else JsonResponse(bundle)


@sync_to_async
@login_required
@async_to_sync
async def search(request, owner, name):
    repo = await get_repo(owner, name, request.user.profile.github_token)
    return HttpResponseNotFound() if repo is None else JsonResponse(repo)


@sync_to_async
@login_required
@async_to_sync
async def refresh(request, owner, name):
    try:
        workflow = await sync_to_async(Workflow.objects.get)(repo_owner=owner, repo_name=name)
    except:
        return HttpResponseNotFound()

    redis = RedisClient.get()
    bundle = await bind_workflow_bundle(workflow, request.user.profile.github_token)
    redis.set(f"workflows/{owner}/{name}", json.dumps(bundle))
    logger.info(f"Refreshed workflow {owner}/{name}")
    return JsonResponse(bundle)


# @sync_to_async
@login_required
# @async_to_sync
def readme(request, owner, name):
    rm = get_repo_readme(name, owner, request.user.profile.github_token)
    return JsonResponse({'readme': rm})


@sync_to_async
@login_required
@async_to_sync
async def toggle_public(request, owner, name):
    if owner != request.user.profile.github_username:
        return HttpResponseForbidden()

    try:
        workflow = await sync_to_async(Workflow.objects.get)(user=request.user, repo_owner=owner, repo_name=name)
    except:
        return HttpResponseNotFound()

    redis = RedisClient.get()
    workflow.public = not workflow.public
    workflow.save()
    bundle = await bind_workflow_bundle(workflow, request.user.profile.github_token)
    redis.set(f"workflows/{owner}/{name}", json.dumps(bundle))
    logger.info(f"Workflow {owner}/{name} is now {'public' if workflow.public else 'private'}")
    return JsonResponse({'workflows': [json.loads(redis.get(key)) for key in redis.scan_iter(match=f"workflows/{owner}/*")]})


@login_required
def bind(request, owner, name):
    if owner != request.user.profile.github_username:
        return HttpResponseForbidden()

    redis = RedisClient.get()
    body = json.loads(request.body.decode('utf-8'))
    body['bound'] = True
    redis.set(f"workflows/{owner}/{name}", json.dumps(body))
    Workflow.objects.create(user=request.user, repo_owner=owner, repo_name=name, public=False)
    logger.info(f"Created binding for workflow {owner}/{name} as {body['config']['name']}")
    return JsonResponse({'workflows': [json.loads(redis.get(key)) for key in redis.scan_iter(match=f"workflows/{owner}/*")]})


@login_required
def unbind(request, owner, name):
    if owner != request.user.profile.github_username:
        return HttpResponseForbidden()

    try:
        workflow = Workflow.objects.get(user=request.user, repo_owner=owner, repo_name=name)
    except:
        return HttpResponseNotFound()

    workflow.delete()
    redis = RedisClient.get()
    cached = json.loads(redis.get(f"workflows/{owner}/{name}"))
    cached['public'] = False
    cached['bound'] = False
    redis.set(f"workflows/{owner}/{name}", json.dumps(cached))
    logger.info(f"Removed binding for workflow {owner}/{name}")
    return JsonResponse({'workflows': [json.loads(redis.get(key)) for key in redis.scan_iter(match=f"workflows/{owner}/*")]})
