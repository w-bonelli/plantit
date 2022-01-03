import asyncio
import fileinput
import json
import logging
import os
import subprocess
import sys
import tempfile
import traceback
import uuid
from collections import Counter
from datetime import timedelta, datetime
from os import environ
from os.path import isdir
from os.path import join
from pathlib import Path
from typing import List
from urllib.parse import quote_plus

import binascii
import jwt
import numpy as np
import plantit.github as github
import plantit.terrain as terrain
import requests
import yaml
from asgiref.sync import async_to_sync
from asgiref.sync import sync_to_async
from channels.layers import get_channel_layer
from dateutil import parser
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import MultipleObjectsReturned
from django.db.models import Count
from django.utils import timezone
from django_celery_beat.models import IntervalSchedule, PeriodicTasks
from math import ceil
from paramiko.ssh_exception import SSHException
from plantit.agents.models import Agent, AgentAccessPolicy, AgentRole, AgentExecutor, AgentTask, AgentAuthentication
from plantit.datasets.models import DatasetAccessPolicy
from plantit.docker import parse_image_components, image_exists
from plantit.miappe.models import Investigation, Study
from plantit.misc import del_none, format_bind_mount, parse_bind_mount
from plantit.news.models import NewsUpdate
from plantit.notifications.models import Notification
from plantit.redis import RedisClient
from plantit.ssh import SSH, execute_command
from plantit.scp import SCPClient
from plantit.tasks.models import DelayedTask, RepeatingTask, TaskStatus, TaskCounter
from plantit.tasks.models import Task
from plantit.tasks.options import BindMount, EnvironmentVariable
from plantit.tasks.options import PlantITCLIOptions, Parameter, Input, PasswordTaskAuth, KeyTaskAuth, InputKind
from plantit.users.models import Profile
from requests import RequestException, ReadTimeout, Timeout, HTTPError
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

from plantit import settings

logger = logging.getLogger(__name__)


# users

def list_users(invalidate: bool = False) -> List[dict]:
    redis = RedisClient.get()
    updated = redis.get(f"users_updated")

    # repopulate if empty or invalidation requested
    if updated is None or len(list(redis.scan_iter(match=f"users/*"))) == 0 or invalidate:
        refresh_user_cache()
    else:
        age = (datetime.now() - datetime.fromtimestamp(float(updated)))
        age_secs = age.total_seconds()
        max_secs = (int(settings.USERS_REFRESH_MINUTES) * 60)

        # otherwise only if stale
        if age_secs > max_secs:
            logger.info(f"User cache is stale ({age_secs}s old, {age_secs - max_secs}s past limit), repopulating")
            refresh_user_cache()

    return [json.loads(redis.get(key)) for key in redis.scan_iter(match=f"users/*")]


def refresh_user_cache():
    logger.info(f"Refreshing user cache")
    redis = RedisClient.get()
    for user in list(User.objects.all().exclude(profile__isnull=True)):
        bundle = get_user_bundle(user)
        redis.set(f"users/{user.username}", json.dumps(bundle))
    RedisClient.get().set(f"users_updated", timezone.now().timestamp())


@sync_to_async
def list_user_projects(user: User):
    return list(user.project_teams.all()) + list(Investigation.objects.filter(owner=user))


def get_user_bundle(user: User):
    if not user.profile.github_username:
        return {
            'username': user.username,
            'first_name': user.first_name,
            'last_name': user.last_name,
        }
    else:
        # TODO in the long run we should probably hide all model access/caching behind a data layer, but for now cache here
        redis = RedisClient.get()
        cached = redis.get(f"users/{user.username}")
        if cached is not None: return json.loads(cached)
        github_profile = async_to_sync(get_user_github_profile)(user)
        github_organizations = async_to_sync(get_user_github_organizations)(user)
        bundle = {
                    'username': user.username,
                    'first_name': user.first_name,
                    'last_name': user.last_name,
                    'github_username': user.profile.github_username,
                    'github_profile': github_profile,
                    'github_organizations': github_organizations,
                } if 'login' in github_profile else {
            'username': user.username,
            'first_name': user.first_name,
            'last_name': user.last_name,
        }
        redis.set(f"users/{user.username}", bundle)
        return bundle


@sync_to_async
def get_profile_user(profile: Profile):
    return profile.user


@sync_to_async
def get_user_django_profile(user: User):
    profile = Profile.objects.get(user=user)
    return profile


def get_user_cyverse_profile(user: User) -> dict:
    profile = terrain.get_profile(user.username, user.profile.cyverse_access_token)
    altered = False

    if profile['first_name'] != user.first_name:
        user.first_name = profile['first_name']
        altered = True
    if profile['last_name'] != user.last_name:
        user.last_name = profile['last_name']
        altered = True
    if profile['institution'] != user.profile.institution:
        user.profile.institution = profile['institution']
        altered = True

    if altered:
        user.profile.save()
        user.save()

    return profile


def refresh_user_cyverse_tokens(user: User):
    access_token, refresh_token = terrain.refresh_tokens(username=user.username,
                                                         refresh_token=user.profile.cyverse_refresh_token)
    user.profile.cyverse_access_token = access_token
    user.profile.cyverse_refresh_token = refresh_token
    user.profile.save()
    user.save()


async def get_user_github_profile(user: User):
    profile = await sync_to_async(Profile.objects.get)(user=user)
    return await github.get_profile(profile.github_username, profile.github_token)


async def get_user_github_organizations(user: User):
    profile = await sync_to_async(Profile.objects.get)(user=user)
    return await github.list_user_organizations(profile.github_username, profile.github_token)


@sync_to_async
def filter_tasks(user: User, completed: bool = None):
    if completed is not None and completed:
        tasks = Task.objects.filter(user=user, completed__isnull=False)
    else:
        tasks = Task.objects.filter(user=user)
    return list(tasks)


@sync_to_async
def filter_agents(user: User = None, guest: User = None):
    if user is not None and guest is None:
        return list(Agent.objects.filter(user=user))
    elif user is None and guest is not None:
        return list(Agent.objects.filter(users_authorized__username=guest.username))
    else:
        raise ValueError(f"Expected either user or guest to be None")


def filter_online(users: List[User]) -> List[User]:
    online = []

    for user in users:
        decoded_token = jwt.decode(user.profile.cyverse_access_token, options={
            'verify_signature': False,
            'verify_aud': False,
            'verify_iat': False,
            'verify_exp': False,
            'verify_iss': False
        })
        exp = datetime.fromtimestamp(decoded_token['exp'], timezone.utc)
        now = datetime.now(tz=timezone.utc)

        if now > exp:
            print(f"Session for {decoded_token['preferred_username']} expired at {exp.isoformat()}")
        else:
            print(f"Session for {decoded_token['preferred_username']} valid until {exp.isoformat()}")
            online.append(user)

    return online


def get_user_statistics(user: User) -> dict:
    redis = RedisClient.get()
    stats_last_updated = redis.get(f"stats_updated/{user.username}")

    # if we haven't aggregated stats for this user before, do it now
    if stats_last_updated is None:
        logger.info(f"No usage statistics for {user.username}. Aggregating stats...")
        stats = async_to_sync(calculate_user_statistics)(user)
        redis = RedisClient.get()
        redis.set(f"stats/{user.username}", json.dumps(stats))
        redis.set(f"stats_updated/{user.username}", datetime.now().timestamp())
    else:
        stats = redis.get(f"stats/{user.username}")
        stats = json.loads(stats) if stats is not None else None

    return stats


def get_users_timeseries():
    series = []
    for i, user in enumerate(User.objects.all().order_by('profile__created')): series.append((user.profile.created.isoformat(), i + 1))

    # update cache
    redis = RedisClient.get()
    redis.set(f"users_timeseries", json.dumps(series))

    return series


def get_tasks_timeseries():
    series = []
    for i, task in enumerate(Task.objects.all().order_by('created')): series.append((task.created.isoformat(), i + 1))

    # update cache
    redis = RedisClient.get()
    redis.set(f"tasks_timeseries", json.dumps(series))

    return series


def get_tasks_running_timeseries(interval_seconds: int = 600, user: User = None):
    tasks = Task.objects.all() if user is None else Task.objects.filter(user=user).order_by('completed')
    series = dict()

    # return early if no tasks
    if len(tasks) == 0:
        return series

    # find the running interval for each task
    start_end_times = dict()
    for task in tasks:
        start_end_times[task.guid] = (task.created, task.completed if task.completed is not None else timezone.now())

    # count the number of running tasks for each value in the time domain
    start = min([v[0] for v in start_end_times.values()])
    end = max(v[1] for v in start_end_times.values())
    for t in range(int(start.timestamp()), int(end.timestamp()), interval_seconds):
        running = len([1 for k, se in start_end_times.items() if int(se[0].timestamp()) <= t <= int(se[1].timestamp())])
        series[datetime.fromtimestamp(t).isoformat()] = running

    # update cache
    redis = RedisClient.get()
    redis.set(f"user_tasks_running/{user.username}" if user is not None else 'tasks_running', json.dumps(series))

    return series


async def calculate_user_statistics(user: User) -> dict:
    profile = await sync_to_async(Profile.objects.get)(user=user)
    all_tasks = await filter_tasks(user=user)
    completed_tasks = await filter_tasks(user=user, completed=True)
    total_tasks = len(all_tasks)
    total_time = sum([(task.completed - task.created).total_seconds() for task in completed_tasks])
    total_results = sum([len(task.results if task.results is not None else []) for task in completed_tasks])
    owned_workflows = [
        f"{workflow['repo']['owner']['login']}/{workflow['config']['name'] if 'name' in workflow['config'] else '[unnamed]'}"
        for
        workflow in list_user_workflows(owner=profile.github_username)] if profile.github_username != '' else []
    used_workflows = [f"{task.workflow_owner}/{task.workflow_name}" for task in all_tasks]
    used_workflows_counter = Counter(used_workflows)
    unique_used_workflows = list(np.unique(used_workflows))
    owned_agents = [(await sync_to_async(agent_to_dict)(agent, user))['name'] for agent in
                    [agent for agent in await filter_agents(user=user) if agent is not None]]
    guest_agents = [(await sync_to_async(agent_to_dict)(agent, user))['name'] for agent in
                    [agent for agent in await filter_agents(user=user) if agent is not None]]
    used_agents = [(await sync_to_async(agent_to_dict)(agent, user))['name'] for agent in
                   [a for a in [await get_task_agent(task) for task in all_tasks] if a is not None]]
    used_agents_counter = Counter(used_agents)
    unique_used_agents = list(np.unique(used_agents))
    # owned_datasets = terrain.list_dir(f"/iplant/home/{user.username}", profile.cyverse_access_token)
    # guest_datasets = terrain.list_dir(f"/iplant/home/", profile.cyverse_access_token)
    tasks_running = await sync_to_async(get_tasks_running_timeseries)(600, user)

    return {
        'total_tasks': total_tasks,
        'total_task_seconds': total_time,
        'total_task_results': total_results,
        'owned_workflows': owned_workflows,
        'workflow_usage': {
            'values': [used_workflows_counter[workflow] for workflow in unique_used_workflows],
            'labels': unique_used_workflows,
        },
        'agent_usage': {
            'values': [used_agents_counter[agent] for agent in unique_used_agents],
            'labels': unique_used_agents,
        },
        'task_status': {
            'values': [1 if task.status == 'success' else 0 for task in all_tasks],
            'labels': ['SUCCESS' if task.status == 'success' else 'FAILURE' for task in all_tasks],
        },
        'owned_agents': owned_agents,
        'guest_agents': guest_agents,
        'institution': profile.institution,
        'tasks_running': tasks_running
    }


def get_or_create_user_keypair(username: str, overwrite: bool = False) -> str:
    """
    Creates an RSA-protected SSH keypair for the user and returns the public key (or gets the public key if a keypair already exists).
    To overwrite a pre-existing keypair, use the `invalidate` argument.

    Args:
        username: The user (CyVerse/Django username) to create a keypair for.
        overwrite: Whether to overwrite an existing keypair.

    Returns: The path to the newly created public key.
    """
    public_key_path = get_user_public_key_path(username)
    private_key_path = get_user_private_key_path(username)

    if public_key_path.is_file():
        if overwrite:
            logger.info(f"Keypair for {username} already exists, overwriting")
            public_key_path.unlink()
            private_key_path.unlink(missing_ok=True)
        else:
            logger.info(f"Keypair for {username} already exists")
    else:
        subprocess.run(f"ssh-keygen -b 2048 -t rsa -f {private_key_path} -N \"\"", shell=True)
        logger.info(f"Created keypair for {username}")

    with open(public_key_path, 'r') as key:
        return key.readlines()[0]


def get_user_private_key_path(username: str) -> Path:
    path = Path(f"{Path(settings.AGENT_KEYS).absolute()}/{username}")
    path.mkdir(exist_ok=True, parents=True)
    return Path(join(path, f"{username}_id_rsa"))


def get_user_public_key_path(username: str) -> Path:
    path = Path(f"{Path(settings.AGENT_KEYS).absolute()}/{username}")
    path.mkdir(exist_ok=True, parents=True)
    return Path(join(path, f"{username}_id_rsa.pub"))


def repopulate_institutions_cache():
    redis = RedisClient.get()
    institution_counts = list(
        Profile.objects.exclude(institution__exact='').values('institution').annotate(Count('institution')))

    for institution_count in institution_counts:
        institution = institution_count['institution']
        count = institution_count['institution__count']

        place = quote_plus(institution)
        response = requests.get(
            f"https://api.mapbox.com/geocoding/v5/mapbox.places/{place}.json?access_token={settings.MAPBOX_TOKEN}")
        content = response.json()
        feature = content['features'][0]
        feature['id'] = institution
        feature['properties'] = {
            'name': institution,
            'count': count
        }
        redis.set(f"institutions/{institution}", json.dumps({
            'institution': institution,
            'count': count,
            'geocode': feature
        }))

    redis.set("institutions_updated", timezone.now().timestamp())


def list_institutions(invalidate: bool = False) -> List[dict]:
    redis = RedisClient.get()
    updated = redis.get('institutions_updated')

    # repopulate if empty or invalidation requested
    if updated is None or len(list(redis.scan_iter(match=f"institutions/*"))) == 0 or invalidate:
        logger.info(f"Populating user institution cache")
        repopulate_institutions_cache()
    else:
        age = (datetime.now() - datetime.fromtimestamp(float(updated)))
        age_secs = age.total_seconds()
        max_secs = (int(settings.MAPBOX_FEATURE_REFRESH_MINUTES) * 60)

        # otherwise only if stale
        if age_secs > max_secs:
            logger.info(
                f"User institution cache is stale ({age_secs}s old, {age_secs - max_secs}s past limit), repopulating")
            repopulate_institutions_cache()

    return [json.loads(redis.get(key)) for key in redis.scan_iter(match='institutions/*')]


# workflows


async def refresh_online_users_workflow_cache():
    users = await sync_to_async(User.objects.all)()
    online = await sync_to_async(filter_online)(users)
    logger.info(f"Refreshing workflow cache for {len(online)} online user(s)")
    for user in online:
        profile = await sync_to_async(Profile.objects.get)(user=user)
        await refresh_user_workflow_cache(profile.github_username)


async def refresh_user_workflow_cache(github_username: str):
    if github_username is None or github_username == '': raise ValueError(f"No GitHub username provided")

    try:
        profile = await sync_to_async(Profile.objects.get)(github_username=github_username)
        user = await get_profile_user(profile)
    except MultipleObjectsReturned:
        logger.warning(f"Multiple users bound to Github user {github_username}!")
        return
    except:
        logger.warning(f"Github user {github_username} does not exist")
        return

    # scrape GitHub to synchronize repos and workflow config
    profile = await sync_to_async(Profile.objects.get)(user=user)
    workflows = await github.list_connectable_repos_by_owner(github_username, profile.github_token)

    # update the cache, first removing workflows that no longer exist
    redis = RedisClient.get()
    cursor = '0'
    removed = 0
    existing = 0
    while cursor != 0:
        cursor, data = redis.scan(cursor, f"workflows/{github_username}")
        wf_keys = [f"workflows/{github_username}/{wf['repo']['name']}/{wf['branch']['name']}" for wf in workflows]
        for key in data:
            decoded = key.decode('utf-8')
            if decoded not in wf_keys:
                removed += 1
                redis.delete(decoded)
            else:
                existing += 1
    # ...then adding/updating the workflows we just scraped
    for wf in workflows:
        redis.set(f"workflows/{github_username}/{wf['repo']['name']}/{wf['branch']['name']}", json.dumps(del_none(wf)))
    redis.set(f"workflows_updated/{github_username}", timezone.now().timestamp())
    logger.info(
        f"{len(workflows)} workflow(s) now in GitHub user's {github_username}'s workflow cache (added {len(workflows) - existing} new, removed {removed}, updated {existing})")


async def refresh_online_user_orgs_workflow_cache():
    users = await sync_to_async(User.objects.all)()
    online = await sync_to_async(filter_online)(users)
    for user in online:
        profile = await sync_to_async(Profile.objects.get)(user=user)
        github_organizations = await get_user_github_organizations(user)
        logger.info(f"Refreshing workflow cache for online user {user.username}'s {len(online)} organizations")
        for org in github_organizations:
            await refresh_org_workflow_cache(org['login'], profile.github_token)


async def refresh_org_workflow_cache(org_name: str, github_token: str):
    # scrape GitHub to synchronize repos and workflow config
    workflows = await github.list_connectable_repos_by_org(org_name, github_token)

    # update the cache, first removing workflows that no longer exist
    redis = RedisClient.get()
    cursor = '0'
    removed = 0
    existing = 0
    while cursor != 0:
        cursor, data = redis.scan(cursor, f"workflows/{org_name}")
        wf_keys = [f"workflows/{org_name}/{wf['repo']['name']}/{wf['branch']['name']}" for wf in workflows]
        for key in data:
            decoded = key.decode('utf-8')
            if decoded not in wf_keys:
                removed += 1
                redis.delete(decoded)
            else:
                existing += 1
    # ...then adding/updating the workflows we just scraped
    for wf in workflows: redis.set(f"workflows/{org_name}/{wf['repo']['name']}/{wf['branch']['name']}", json.dumps(del_none(wf)))
    redis.set(f"workflows_updated/{org_name}", timezone.now().timestamp())
    logger.info(f"{len(workflows)} workflow(s) in GitHub organization {org_name}'s workflow cache")


def list_public_workflows() -> List[dict]:
    redis = RedisClient.get()
    workflows = [wf for wf in [json.loads(redis.get(key)) for key in redis.scan_iter(match='workflows/*')] if
                 'public' in wf['config'] and wf['config']['public']]
    return workflows


def list_user_workflows(owner: str) -> List[dict]:
    redis = RedisClient.get()
    return [json.loads(redis.get(key)) for key in redis.scan_iter(match=f"workflows/{owner}/*")]


async def get_workflow(
        owner: str,
        name: str,
        branch: str,
        github_token: str,
        cyverse_token: str,
        invalidate: bool = False) -> dict:
    redis = RedisClient.get()
    last_updated = redis.get(f"workflows_updated/{owner}")
    workflow = redis.get(f"workflows/{owner}/{name}/{branch}")

    if last_updated is None or workflow is None or invalidate:
        bundle = await github.get_repo_bundle(owner, name, branch, github_token, cyverse_token)
        workflow = {
            'config': bundle['config'],
            'repo': bundle['repo'],
            'validation': bundle['validation'],
            'branch': branch
        }
        redis.set(f"workflows/{owner}/{name}/{branch}", json.dumps(del_none(workflow)))
        return workflow
    else:
        return json.loads(workflow)


# def empty_user_workflow_cache(owner: str):
#     redis = RedisClient.get()
#     keys = list(redis.scan_iter(match=f"workflows/{owner}/*"))
#     cleaned = len(keys)
#     for key in keys: redis.delete(key)
#     logger.info(f"Emptied {cleaned} workflows from GitHub user {owner}'s cache")
#     return cleaned


# tasks

@sync_to_async
def get_task_user(task: Task):
    return task.user


@sync_to_async
def get_task_agent(task: Task):
    return task.agent


def parse_task_walltime(walltime) -> timedelta:
    time_split = walltime.split(':')
    time_hours = int(time_split[0])
    time_minutes = int(time_split[1])
    time_seconds = int(time_split[2])
    return timedelta(hours=time_hours, minutes=time_minutes, seconds=time_seconds)


def parse_task_job_id(line: str) -> str:
    try:
        return str(int(line.replace('Submitted batch job', '').strip()))
    except:
        raise Exception(f"Failed to parse job ID from '{line}'\n{traceback.format_exc()}")


def parse_task_time(data: dict) -> datetime:
    time_str = data['time']
    time = parser.isoparse(time_str)
    return time


def parse_task_eta(data: dict) -> (datetime, int):
    delay_value = data['delayValue']
    delay_units = data['delayUnits']

    if delay_units == 'Seconds':
        seconds = int(delay_value)
    elif delay_units == 'Minutes':
        seconds = int(delay_value) * 60
    elif delay_units == 'Hours':
        seconds = int(delay_value) * 60 * 60
    elif delay_units == 'Days':
        seconds = int(delay_value) * 60 * 60 * 24
    else:
        raise ValueError(f"Unsupported delay units (expected: Seconds, Minutes, Hours, or Days)")

    now = timezone.now()
    eta = now + timedelta(seconds=seconds)

    return eta, seconds


def parse_task_cli_options(task: Task) -> (List[str], PlantITCLIOptions):
    config = task.workflow['config']
    config['workdir'] = join(task.agent.workdir, task.guid)
    config['log_file'] = f"{task.guid}.{task.agent.name.lower()}.log"

    # set the output directory (if none is set, use the task working dir)
    default_from = join(task.agent.workdir, task.workdir)
    if 'output' in config:
        if 'from' in config['output']:
            if config['output']['from'] is not None and config['output']['from'] != '':
                config['output']['from'] = join(task.agent.workdir, task.workdir, config['output']['from'])
            else:
                config['output']['from'] = default_from
        else:
            config['output']['from'] = default_from
    else:
        config['output'] = dict()
        config['output']['from'] = default_from

    if 'include' not in config['output']: config['output']['include'] = dict()
    if 'patterns' not in config['output']['include']: config['output']['exclude']['patterns'] = []

    # include task configuration file and scheduler logs
    config['output']['include']['names'].append(f"{task.guid}.yaml")
    config['output']['include']['patterns'].append("out")
    config['output']['include']['patterns'].append("err")
    config['output']['include']['patterns'].append("log")

    if 'exclude' not in config['output']: config['output']['exclude'] = dict()
    if 'names' not in config['output']['exclude']: config['output']['exclude']['names'] = []

    # exclude template scripts
    config['output']['exclude']['names'].append("template_task_local.sh")
    config['output']['exclude']['names'].append("template_task_slurm.sh")
    output = config['output']

    errors = []
    image = None
    if not isinstance(config['image'], str):
        errors.append('Attribute \'image\' must not be a str')
    elif config['image'] == '':
        errors.append('Attribute \'image\' must not be empty')
    else:
        image = config['image']
        if 'docker' in image:
            image_owner, image_name, image_tag = parse_image_components(image)
            if not image_exists(image_name, image_owner, image_tag):
                errors.append(f"Image '{image}' not found on Docker Hub")

    work_dir = None
    if not isinstance(config['workdir'], str):
        errors.append('Attribute \'workdir\' must not be a str')
    elif config['workdir'] == '':
        errors.append('Attribute \'workdir\' must not be empty')
    else:
        work_dir = config['workdir']

    command = None
    if not isinstance(config['commands'], str):
        errors.append('Attribute \'commands\' must not be a str')
    elif config['commands'] == '':
        errors.append('Attribute \'commands\' must not be empty')
    else:
        command = config['commands']

    env = []
    if 'env' in config:
        if not all(var != '' for var in config['env']):
            errors.append('Every environment variable must be non-empty')
        else:
            env = [EnvironmentVariable(
                key=variable.rpartition('=')[0],
                value=variable.rpartition('=')[2])
                for variable in config['env']]

    parameters = None
    if 'parameters' in config:
        if not all(['name' in param and
                    param['name'] is not None and
                    param['name'] != '' and
                    'value' in param and
                    param['value'] is not None and
                    param['value'] != ''
                    for param in config['parameters']]):
            errors.append('Every parameter must have a non-empty \'name\' and \'value\'')
        else:
            parameters = [Parameter(key=param['name'], value=param['value']) for param in config['parameters']]

    bind_mounts = None
    if 'bind_mounts' in config:
        if not all(mount_point != '' for mount_point in config['bind_mounts']):
            errors.append('Every mount point must be non-empty')
        else:
            bind_mounts = [parse_bind_mount(work_dir, mount_point) for mount_point in config['bind_mounts']]

    input = None
    if 'input' in config:
        if 'kind' not in config['input']:
            errors.append("Section \'input\' must include attribute \'kind\'")
        if 'path' not in config['input']:
            errors.append("Section \'input\' must include attribute \'path\'")

        kind = config['input']['kind']
        path = config['input']['path']
        if kind == 'file':
            input = Input(path=path, kind='file')
        elif kind == 'files':
            input = Input(path=path, kind='files',
                          patterns=config['input']['patterns'] if 'patterns' in config['input'] else None)
        elif kind == 'directory':
            input = Input(path=path, kind='directory',
                          patterns=config['input']['patterns'] if 'patterns' in config['input'] else None)
        else:
            errors.append('Section \'input.kind\' must be \'file\', \'files\', or \'directory\'')

    log_file = None
    if 'log_file' in config:
        log_file = config['log_file']
        if not isinstance(log_file, str):
            errors.append('Attribute \'log_file\' must be a str')
        elif log_file.rpartition('/')[0] != '' and not isdir(log_file.rpartition('/')[0]):
            errors.append('Attribute \'log_file\' must be a valid file path')

    no_cache = None
    if 'no_cache' in config:
        no_cache = config['no_cache']
        if not isinstance(no_cache, bool):
            errors.append('Attribute \'no_cache\' must be a bool')

    gpu = None
    if 'gpu' in config:
        gpu = config['gpu']
        if not isinstance(gpu, bool):
            errors.append('Attribute \'gpu\' must be a bool')

    jobqueue = None
    if 'jobqueue' in config:
        jobqueue = config['jobqueue']
        # if not (
        #         'slurm' in jobqueue or 'yarn' in jobqueue or 'pbs' in jobqueue or 'moab' in jobqueue or 'sge' in jobqueue or 'lsf' in jobqueue or 'oar' in jobqueue or 'kube' in jobqueue):
        #     raise ValueError(f"Unsupported jobqueue configuration: {jobqueue}")

        if 'queue' in jobqueue:
            if not isinstance(jobqueue['queue'], str):
                errors.append('Section \'jobqueue\'.\'queue\' must be a str')
        else:
            jobqueue['queue'] = task.agent.queue
        if 'project' in jobqueue:
            if not isinstance(jobqueue['project'], str):
                errors.append('Section \'jobqueue\'.\'project\' must be a str')
        elif task.agent.project is not None and task.agent.project != '':
            jobqueue['project'] = task.agent.project
        if 'walltime' in jobqueue:
            if not isinstance(jobqueue['walltime'], str):
                errors.append('Section \'jobqueue\'.\'walltime\' must be a str')
        else:
            jobqueue['walltime'] = task.agent.max_walltime
        if 'cores' in jobqueue:
            if not isinstance(jobqueue['cores'], int):
                errors.append('Section \'jobqueue\'.\'cores\' must be a int')
        else:
            jobqueue['cores'] = task.agent.max_cores
        if 'processes' in jobqueue:
            if not isinstance(jobqueue['processes'], int):
                errors.append('Section \'jobqueue\'.\'processes\' must be a int')
        else:
            jobqueue['processes'] = task.agent.max_processes
        if 'header_skip' in jobqueue and not all(extra is str for extra in jobqueue['header_skip']):
            errors.append('Section \'jobqueue\'.\'header_skip\' must be a list of str')
        elif task.agent.header_skip is not None and task.agent.header_skip != '':
            jobqueue['header_skip'] = task.agent.header_skip
        if 'extra' in jobqueue and not all(extra is str for extra in jobqueue['extra']):
            errors.append('Section \'jobqueue\'.\'extra\' must be a list of str')

    options = PlantITCLIOptions(
        workdir=work_dir,
        image=image,
        command=command)

    if input is not None: options['input'] = input
    if output is not None: options['output'] = output
    if parameters is not None: options['parameters'] = parameters
    if env is not None: options['env'] = env
    if bind_mounts is not None: options['bind_mounts'] = bind_mounts
    # if checksums is not None: options['checksums'] = checksums
    if log_file is not None: options['log_file'] = log_file
    if jobqueue is not None: options['jobqueue'] = jobqueue
    if no_cache is not None: options['no_cache'] = no_cache
    if gpu is not None: options['gpus'] = task.agent.gpus

    return errors, options


def parse_time_limit_seconds(time):
    time_limit = time['limit']
    time_units = time['units']
    seconds = time_limit
    if time_units == 'Days':
        seconds = seconds * 60 * 60 * 24
    elif time_units == 'Hours':
        seconds = seconds * 60 * 60
    elif time_units == 'Minutes':
        seconds = seconds * 60
    return seconds


def create_task(username: str,
                agent_name: str,
                workflow: dict,
                branch: dict,
                name: str = None,
                guid: str = None,
                project: str = None,
                study: str = None):
    repo_owner = workflow['repo']['owner']['login']
    repo_name = workflow['repo']['name']
    repo_branch = branch['name']
    agent = Agent.objects.get(name=agent_name)
    user = User.objects.get(username=username)
    if guid is None: guid = str(uuid.uuid4())  # if the browser client hasn't set a GUID, create one
    now = timezone.now()

    time_limit = parse_time_limit_seconds(workflow['config']['time'])
    logger.info(f"Using task time limit {time_limit}s")
    due_time = timezone.now() + timedelta(seconds=time_limit)

    task = Task.objects.create(
        guid=guid,
        name=guid if name is None else name,
        user=user,
        workflow=workflow,
        workflow_owner=repo_owner,
        workflow_name=repo_name,
        workflow_branch=repo_branch,
        agent=agent,
        status=TaskStatus.CREATED,
        created=now,
        updated=now,
        due_time=due_time,
        token=binascii.hexlify(os.urandom(20)).decode())

    # add MIAPPE info
    if project is not None: task.project = Investigation.objects.get(owner=user, title=project)
    if study is not None: task.study = Study.objects.get(project=task.project, title=study)

    # add repo logo
    if 'logo' in workflow['config']:
        logo_path = workflow['config']['logo']
        task.workflow_image_url = f"https://raw.githubusercontent.com/{repo_owner}/{repo_name}/master/{logo_path}"

    for tag in workflow['config']['tags']: task.tags.add(tag)  # add task tags
    task.workdir = f"{task.guid}/"  # use GUID for working directory name
    task.save()

    counter = TaskCounter.load()
    counter.count = counter.count + 1
    counter.save()

    return task


def configure_local_task_environment(task: Task, ssh: SSH):
    log_task_orchestrator_status(task, [f"Verifying configuration"])
    async_to_sync(push_task_event)(task)

    parse_errors, cli_options = parse_task_cli_options(task)
    if len(parse_errors) > 0: raise ValueError(f"Failed to parse task options: {' '.join(parse_errors)}")

    work_dir = join(task.agent.workdir, task.guid)
    log_task_orchestrator_status(task, [f"Creating working directory: {work_dir}"])
    async_to_sync(push_task_event)(task)

    list(execute_command(ssh=ssh, precommand=':', command=f"mkdir {task.guid}", directory=task.agent.workdir))

    log_task_orchestrator_status(task, [f"Uploading task"])
    upload_task_executables(task, ssh, cli_options)
    async_to_sync(push_task_event)(task)

    with ssh.client.open_sftp() as sftp:
        # TODO remove this utter hack
        if 'input' in cli_options:
            kind = cli_options['input']['kind']
            path = cli_options['input']['path']

            files = list_task_input_files(task, cli_options) if kind == 'files' else []
            task.inputs_detected = len(files)
            task.save()

            if kind == InputKind.DIRECTORY or kind == InputKind.FILES:
                log_task_orchestrator_status(task, [f"Checking for input files"])
                async_to_sync(push_task_event)(task)

                # !!!
                cli_options['input']['path'] = 'input'
            else:
                cli_options['input']['path'] = f"input/{path.rpartition('/')[2]}"

        del cli_options['jobqueue']

        sftp.chdir(work_dir)
        with sftp.open(f"{task.guid}.yaml", 'w') as cli_file:
            yaml.dump(del_none(cli_options), cli_file, default_flow_style=False)
        if 'input' in cli_options: sftp.mkdir(join(work_dir, 'input'))


def configure_jobqueue_task_environment(task: Task, ssh: SSH, auth: dict):
    log_task_orchestrator_status(task, [f"Verifying configuration"])
    async_to_sync(push_task_event)(task)

    parse_errors, cli_options = parse_task_cli_options(task)

    if len(parse_errors) > 0: raise ValueError(f"Failed to parse task options: {' '.join(parse_errors)}")

    work_dir = join(task.agent.workdir, task.guid)
    log_task_orchestrator_status(task, [f"Creating working directory"])
    async_to_sync(push_task_event)(task)

    list(execute_command(ssh=ssh, precommand=':', command=f"mkdir {work_dir}"))

    log_task_orchestrator_status(task, [f"Uploading task"])
    upload_task_files(task, ssh, cli_options, auth)
    async_to_sync(push_task_event)(task)


def compose_task_singularity_command(
        work_dir: str,
        image: str,
        command: str,
        env: List[EnvironmentVariable] = None,
        bind_mounts: List[BindMount] = None,
        parameters: List[Parameter] = None,
        no_cache: bool = False,
        gpus: int = 0,
        docker_username: str = None,
        docker_password: str = None,
        index: int = None) -> str:
    # build up the command according to the order:
    # - (non-secret) env vars
    # - singularity invocation
    # - bind mounts
    # - cache & gpu options
    #
    # then prepend
    # - parameters
    # - secret env vars

    cmd = ''
    if env is not None:
        if len(env) > 0: cmd += ' '.join([f"SINGULARITYENV_{v['key'].upper().replace(' ', '_')}={v['value']}" for v in env])
        cmd += ' '
    if parameters is None: parameters = []
    if index is not None: parameters.append(Parameter(key='INDEX', value=str(index)))
    parameters.append(Parameter(key='WORKDIR', value=work_dir))
    for parameter in parameters:
        key = parameter['key'].upper().replace(' ', '_')
        val = str(parameter['value'])
        cmd += f" SINGULARITYENV_{key}={val}"
        # logger.debug(f"Replacing '{key}' with '{val}'")
        # cmd = cmd.replace(f"${key}", val)
        # cmd = f"SINGULARITYENV_{key}={val} " + cmd
    cmd += f" singularity exec --home {work_dir}"
    if bind_mounts is not None and len(bind_mounts) > 0:
        cmd += (' --bind ' + ','.join([format_bind_mount(work_dir, mount_point) for mount_point in bind_mounts]))
    if no_cache: cmd += ' --disable-cache'
    if gpus: cmd += ' --nv'
    cmd += f" {image} sh -c '{command}'"  # is `sh -c '[the command to run]'` always available/safe?
    logger.debug(f"Using command: '{cmd}'")  # don't want to reveal secrets so log before prepending secret env vars
    if docker_username is not None and docker_password is not None:
        cmd = f"SINGULARITY_DOCKER_USERNAME={docker_username} SINGULARITY_DOCKER_PASSWORD={docker_password} " + cmd

    return cmd


def compose_task_pull_command(task: Task, options: PlantITCLIOptions) -> str:
    if 'input' not in options: return ''
    input = options['input']
    if input is None: return ''
    kind = input['kind']

    if input['kind'] != InputKind.FILE and 'patterns' in input:
        # allow for both spellings of JPG
        patterns = [pattern.lower() for pattern in input['patterns']]
        if 'jpg' in patterns and 'jpeg' not in patterns:
            patterns.append("jpeg")
        elif 'jpeg' in patterns and 'jpg' not in patterns:
            patterns.append("jpg")
    else:
        patterns = []

    command = f"plantit terrain pull \"{input['path']}\"" \
              f" -p \"{join(task.agent.workdir, task.workdir, 'input')}\"" \
              f" {' '.join(['--pattern ' + pattern for pattern in patterns])}" \
              f""f" --terrain_token {task.user.profile.cyverse_access_token}"

    if task.agent.callbacks:
        callback_url = settings.API_URL + 'tasks/' + task.guid + '/status/'
        command += f""f" --plantit_url '{callback_url}' --plantit_token '{task.token}'"

    logger.debug(f"Using pull command: {command}")
    return command


def compose_task_run_commands(task: Task, options: PlantITCLIOptions, inputs: List[str]) -> List[str]:
    docker_username = environ.get('DOCKER_USERNAME', None)
    docker_password = environ.get('DOCKER_PASSWORD', None)
    commands = []

    # if this resource uses TACC's launcher, create a parameter sweep script to invoke the Singularity container
    if task.agent.launcher:
        commands.append(f"export LAUNCHER_WORKDIR={join(task.agent.workdir, task.workdir)}\n")
        commands.append(f"export LAUNCHER_JOB_FILE={os.environ.get('LAUNCHER_SCRIPT_NAME')}\n")
        commands.append("$LAUNCHER_DIR/paramrun\n")
    # otherwise use the CLI
    else:
        command = f"plantit run {task.guid}.yaml"
        if task.agent.job_array and len(inputs) > 0:
            command += f" --slurm_job_array"

        if docker_username is not None and docker_password is not None:
            command += f" --docker_username {docker_username} --docker_password {docker_password}"

        if task.agent.callbacks:
            callback_url = settings.API_URL + 'tasks/' + task.guid + '/status/'
            command += f""f" --plantit_url '{callback_url}' --plantit_token '{task.token}'"

        commands.append(command)

    newline = '\n'
    logger.debug(f"Using CLI commands: {newline.join(commands)}")
    return commands


def compose_task_clean_commands(task: Task) -> str:
    docker_username = environ.get('DOCKER_USERNAME', None)
    docker_password = environ.get('DOCKER_PASSWORD', None)
    cmd = f"plantit clean *.out -p {docker_username} -p {docker_password}"
    cmd += f"\nplantit clean *.err -p {docker_username} -p {docker_password}"

    if task.agent.launcher:
        workdir = join(task.agent.workdir, task.workdir)
        launcher_script = join(workdir, os.environ.get('LAUNCHER_SCRIPT_NAME'))
        cmd += f"\nplantit clean {launcher_script} -p {docker_username} -p {docker_password} "

    return cmd


def compose_task_zip_command(task: Task, options: PlantITCLIOptions) -> str:
    if 'output' in options:
        output = options['output']
    else:
        output = dict()
        output['include'] = dict()
        output['include']['names'] = dict()
        output['include']['patterns'] = dict()
        output['exclude'] = dict()
        output['exclude']['names'] = dict()
        output['exclude']['patterns'] = dict()

    # merge output patterns and files from workflow config
    config = task.workflow['config']
    if 'output' in config:
        if 'include' in config['output']:
            if 'patterns' in config['output']['include']:
                output['include']['patterns'] = list(
                    set(output['include']['patterns'] + task.workflow['config']['output']['include']['patterns']))
            if 'names' in config['output']['include']:
                output['include']['names'] = list(
                    set(output['include']['names'] + task.workflow['config']['output']['include']['names']))
        if 'exclude' in config['output']:
            if 'patterns' in config['output']['exclude']:
                output['exclude']['patterns'] = list(
                    set(output['exclude']['patterns'] + task.workflow['config']['output']['exclude']['patterns']))
            if 'names' in config['output']['exclude']:
                output['exclude']['names'] = list(
                    set(output['exclude']['names'] + task.workflow['config']['output']['exclude']['names']))

    command = f"plantit zip {output['from'] if 'from' in output and output['from'] != '' else '.'} -o . -n {task.guid}"
    logs = [f"{task.guid}.{task.agent.name.lower()}.log"]
    command = f"{command} {' '.join(['--include_pattern ' + pattern for pattern in logs])}"

    if 'include' in output:
        if 'patterns' in output['include']:
            command = f"{command} {' '.join(['--include_pattern ' + pattern for pattern in output['include']['patterns']])}"
        if 'names' in output['include']:
            command = f"{command} {' '.join(['--include_name ' + pattern for pattern in output['include']['names']])}"
    if 'exclude' in output:
        if 'patterns' in output['exclude']:
            command = f"{command} {' '.join(['--exclude_pattern ' + pattern for pattern in output['exclude']['patterns']])}"
        if 'names' in output['exclude']:
            command = f"{command} {' '.join(['--exclude_name ' + pattern for pattern in output['exclude']['names']])}"

    logger.debug(f"Using zip command: {command}")
    return command


def compose_task_push_command(task: Task, options: PlantITCLIOptions) -> str:
    command = ''
    if 'output' not in options: return command
    output = options['output']
    if output is None: return command

    # add push command if we have a destination
    if 'to' in output and output['to'] is not None:
        command = f"plantit terrain push {output['to']} -p {join(task.agent.workdir, task.workdir, output['from'])} "

        # command = command + ' ' + ' '.join(['--include_name ' + name for name in get_included_by_name(task)])
        # command = command + ' ' + ' '.join(['--include_pattern ' + pattern for pattern in get_included_by_pattern(task)])
        # command += f" --terrain_token '{task.user.profile.cyverse_access_token}'"

        if 'include' in output:
            if 'patterns' in output['include']:
                patterns = list(output['include']['patterns'])
                patterns.append('.out')
                patterns.append('.err')
                patterns.append('.zip')
                command = command + ' ' + ' '.join(['--include_pattern ' + pattern for pattern in patterns])
            if 'names' in output['include']:
                command = command + ' ' + ' '.join(
                    ['--include_name ' + pattern for pattern in output['include']['names']])
        if 'exclude' in output:
            if 'patterns' in output['exclude']:
                command = command + ' ' + ' '.join(
                    ['--exclude_pattern ' + pattern for pattern in output['exclude']['patterns']])
            if 'names' in output['exclude']:
                command = command + ' ' + ' '.join(
                    ['--exclude_name ' + pattern for pattern in output['exclude']['names']])

        command += f" --terrain_token '{task.user.profile.cyverse_access_token}'"

    logger.debug(f"Using push command: {command}")
    return command


def compose_task_run_script(task: Task, options: PlantITCLIOptions, template: str) -> List[str]:
    with open(template, 'r') as template_file:
        template_header = [line.strip() for line in template_file if line != '']

    if 'input' in options and options['input'] is not None:
        kind = options['input']['kind']
        path = options['input']['path']
        cyverse_token = task.user.profile.cyverse_access_token
        inputs = [terrain.get_file(path, cyverse_token)] if kind == InputKind.FILE else terrain.list_dir(path,
                                                                                                         cyverse_token)
    else:
        inputs = []

    local = task.agent.executor == AgentExecutor.LOCAL
    resource_requests = [] if local else compose_jobqueue_task_resource_requests(task, options, inputs)
    cli_pull = compose_task_pull_command(task, options)
    cli_run = compose_task_run_commands(task, options, inputs)
    cli_clean = compose_task_clean_commands(task)
    cli_zip = compose_task_zip_command(task, options)
    cli_push = compose_task_push_command(task, options)

    return template_header + \
           resource_requests + \
           [task.agent.pre_commands] + \
           [cli_pull] + \
           cli_run + \
           [cli_clean] + \
           [cli_zip] + \
           [cli_push]


def calculate_node_count(task: Task, inputs: List[str]):
    return 1 if task.agent.launcher else (min(len(inputs), task.agent.max_nodes) if inputs is not None and not task.agent.job_array else 1)


def calculate_walltime(task: Task, options: PlantITCLIOptions, inputs: List[str]):
    jobqueue = options['jobqueue']
    split_time = jobqueue['walltime'].split(':')
    hours = int(split_time[0])
    minutes = int(split_time[1])
    seconds = int(split_time[2])
    walltime = timedelta(hours=hours, minutes=minutes, seconds=seconds)

    # TODO adjust walltime to compensate for inputs processed in parallel [requested walltime * input files / nodes]
    # nodes = calculate_node_count(task, inputs)
    # adjusted = walltime * (len(inputs) / nodes) if len(inputs) > 0 else walltime

    # round up to the nearest hour
    hours = f"{min(ceil(walltime.total_seconds() / 60 / 60), int(int(task.agent.max_walltime) / 60))}"
    if len(hours) == 1: hours = f"0{hours}"
    adjusted_str = f"{hours}:00:00"

    logger.info(f"Using walltime {adjusted_str} for {task.user.username}'s task {task.name}")
    return adjusted_str


def compose_jobqueue_task_resource_requests(task: Task, options: PlantITCLIOptions, inputs: List[str]) -> List[str]:
    nodes = calculate_node_count(task, inputs)
    task.inputs_detected = len(inputs)
    task.save()

    if 'jobqueue' not in options: return []
    gpus = options['gpus'] if 'gpus' in options else 0
    jobqueue = options['jobqueue']
    commands = []

    if 'cores' in jobqueue: commands.append(f"#SBATCH --cpus-per-task={int(jobqueue['cores'])}")
    if 'memory' in jobqueue and not has_virtual_memory(task.agent): commands.append(
        f"#SBATCH --mem={'1GB' if task.agent.orchestrator_queue is not None else jobqueue['memory']}")
    if 'walltime' in jobqueue:
        walltime = calculate_walltime(task, options, inputs)
        async_to_sync(push_task_event)(task)
        task.job_requested_walltime = walltime
        task.save()
        commands.append(f"#SBATCH --time={walltime}")
    if gpus and task.agent.orchestrator_queue is None: commands.append(f"#SBATCH --gres=gpu:{gpus}")
    if task.agent.orchestrator_queue is not None and task.agent.orchestrator_queue != '': commands.append(f"#SBATCH --partition={task.agent.orchestrator_queue}")
    elif task.agent.queue is not None and task.agent.queue != '': commands.append(f"#SBATCH --partition={task.agent.queue}")
    if task.agent.project is not None and task.agent.project != '': commands.append(f"#SBATCH -A {task.agent.project}")
    if len(inputs) > 0 and options['input']['kind'] == 'files':
        if task.agent.job_array: commands.append(f"#SBATCH --array=1-{len(inputs)}")
        commands.append(f"#SBATCH -N {nodes}")
        commands.append(f"#SBATCH --ntasks={min(len(inputs), task.agent.max_cores) if inputs is not None and not task.agent.job_array else 1}")
    else:
        commands.append(f"#SBATCH -N 1")
        commands.append("#SBATCH --ntasks=1")
    commands.append("#SBATCH --mail-type=END,FAIL")
    commands.append(f"#SBATCH --mail-user={task.user.email}")
    commands.append("#SBATCH --output=plantit.%j.out")
    commands.append("#SBATCH --error=plantit.%j.err")

    newline = '\n'
    logger.debug(f"Using resource requests: {newline.join(commands)}")
    return commands


def compose_jobqueue_task_launcher_script(task: Task, options: PlantITCLIOptions) -> List[str]:
    lines = []
    docker_username = environ.get('DOCKER_USERNAME', None)
    docker_password = environ.get('DOCKER_PASSWORD', None)
    # TODO: if workflow is configured for gpu, use the number of gpus configured on the agent
    gpus = options['gpus'] if 'gpus' in options else 0

    if 'input' in options:
        files = list_task_input_files(task, options) if (
                'input' in options and options['input']['kind'] == 'files') else []
        task.inputs_detected = len(files)
        task.save()

        if options['input']['kind'] == 'files':
            for i, file in enumerate(files):
                file_name = file.rpartition('/')[2]
                command = compose_task_singularity_command(
                    work_dir=options['workdir'],
                    image=options['image'],
                    command=options['command'],
                    env=options['env'],
                    parameters=(options['parameters'] if 'parameters' in options else []) + [
                        Parameter(key='INPUT', value=join(options['workdir'], 'input', file_name)),
                        Parameter(key='OUTPUT', value=options['output']['from']),
                        Parameter(key='GPUS', value=str(gpus))],
                    bind_mounts=options['bind_mounts'] if (
                            'bind_mounts' in options and isinstance(options['bind_mounts'], list)) else [],
                    no_cache=options['no_cache'] if 'no_cache' in options else False,
                    gpus=gpus,
                    docker_username=docker_username,
                    docker_password=docker_password,
                    index=i)
                lines.append(command)
        elif options['input']['kind'] == 'directory':
            command = compose_task_singularity_command(
                work_dir=options['workdir'],
                image=options['image'],
                command=options['command'],
                env=options['env'],
                parameters=(options['parameters'] if 'parameters' in options else []) + [
                    Parameter(key='INPUT', value=join(options['workdir'], 'input')),
                    Parameter(key='OUTPUT', value=options['output']['from']),
                    Parameter(key='GPUS', value=str(gpus))],
                bind_mounts=options['bind_mounts'] if 'bind_mounts' in options and isinstance(options['bind_mounts'],
                                                                                              list) else [],
                no_cache=options['no_cache'] if 'no_cache' in options else False,
                gpus=gpus,
                docker_username=docker_username,
                docker_password=docker_password)
            lines.append(command)
        elif options['input']['kind'] == 'file':
            file_name = options['input']['path'].rpartition('/')[2]
            command = compose_task_singularity_command(
                work_dir=options['workdir'],
                image=options['image'],
                command=options['command'],
                env=options['env'],
                parameters=(options['parameters'] if 'parameters' in options else []) + [
                    Parameter(key='INPUT', value=join(options['workdir'], 'input', file_name)),
                    Parameter(key='OUTPUT', value=options['output']['from']),
                    Parameter(key='GPUS', value=str(gpus))],
                bind_mounts=options['bind_mounts'] if 'bind_mounts' in options and isinstance(options['bind_mounts'],
                                                                                              list) else [],
                no_cache=options['no_cache'] if 'no_cache' in options else False,
                gpus=gpus,
                docker_username=docker_username,
                docker_password=docker_password)
            lines.append(command)
    else:
        command = compose_task_singularity_command(
            work_dir=options['workdir'],
            image=options['image'],
            command=options['command'],
            env=options['env'],
            parameters=options['parameters'] if 'parameters' in options else [] + [
                Parameter(key='OUTPUT', value=options['output']['from']),
                Parameter(key='GPUS', value=str(gpus))],
            bind_mounts=options['bind_mounts'] if 'bind_mounts' in options else None,
            no_cache=options['no_cache'] if 'no_cache' in options else False,
            gpus=gpus,
            docker_username=docker_username,
            docker_password=docker_password)
        lines.append(command)

    return lines


def upload_task_files(task: Task, ssh: SSH, options: PlantITCLIOptions, auth: dict):
    work_dir = join(task.agent.workdir, task.workdir)

    # NOTE: paramiko is finicky about connecting to certain hosts.
    # the equivalent paramiko implementation is commented below,
    # but for now we just perform each step manually.
    #
    # issues #239: https://github.com/Computational-Plant-Science/plantit/issues/239
    #
    # misc:
    # - if sftp throws an IOError or complains about filesizes,
    #   it probably means the remote host's disk is full.
    #   could catch the error and show an alert in the UI.

    # if this workflow has input files, create a directory for them
    if 'input' in options:
        logger.info(f"Creating input directory for task {task.guid}")
        list(execute_command(ssh=ssh, precommand=':', command=f"mkdir {join(work_dir, 'input')}"))
        # with ssh.client.open_sftp() as sftp:
        #     sftp.chdir(work_dir)
        #     sftp.mkdir(join(work_dir, 'input'))

    # compose and upload the executable
    task_commands = compose_task_run_script(task, options, environ.get(
        'TASKS_TEMPLATE_SCRIPT_LOCAL') if task.agent.executor == AgentExecutor.LOCAL else environ.get(
        'TASKS_TEMPLATE_SCRIPT_SLURM'))
    with tempfile.NamedTemporaryFile() as task_script:
        for line in task_commands: task_script.write(f"{line}\n".encode('utf-8'))
        task_script.seek(0)
        logger.info(os.stat(task_script.name))
        cmd = f"scp -v -o StrictHostKeyChecking=no -i {str(get_user_private_key_path(task.agent.user.username))} {task_script.name} {auth['username']}@{task.agent.hostname}:{join(work_dir, task.guid)}.sh"
        logger.info(f"Uploading job script for task {task.guid} using command: {cmd}")
        subprocess.run(cmd, shell=True)
        # with SCPClient(ssh.client.get_transport()) as scp:
        #     scp.put(task_script.name, join(work_dir, f"{task.guid}.sh"))
        # with ssh.client.open_sftp() as sftp:
        #     sftp.chdir(work_dir)
        #     sftp.put(task_script.name, f"{task.guid}.sh")
        #     with sftp.open(f"{task.guid}.sh", 'w') as script_file:
        #         for line in task_script.readlines():
        #             script_file.write(line)
        # for line in task_commands:
        #     list(execute_command(ssh, ':', f"echo '{line}\n' >> {task.guid}.sh", work_dir))

    # if the selected agent uses the TACC Launcher, create and upload a parameter sweep script too
    if task.agent.launcher:
        with tempfile.NamedTemporaryFile() as launcher_script:
            launcher_commands = compose_jobqueue_task_launcher_script(task, options)
            for line in launcher_commands: launcher_script.write(f"{line}\n".encode('utf-8'))
            launcher_script.seek(0)
            logger.info(os.stat(launcher_script.name))
            cmd = f"scp -v -o StrictHostKeyChecking=no -i {str(get_user_private_key_path(task.agent.user.username))} {launcher_script.name} {auth['username']}@{task.agent.hostname}:{join(work_dir, os.environ.get('LAUNCHER_SCRIPT_NAME'))}"
            logger.info(f"Uploading launcher script for task {task.guid} using command: {cmd}")
            subprocess.run(cmd, shell=True)
            # with SCPClient(ssh.client.get_transport()) as scp:
            #     scp.put(launcher_script.name, join(work_dir, os.environ.get('LAUNCHER_SCRIPT_NAME')))
            # with ssh.client.open_sftp() as sftp:
            #     sftp.chdir(work_dir)
            #     sftp.put(launcher_script.name, os.environ.get('LAUNCHER_SCRIPT_NAME'))
            #     with sftp.open("launch.sh", 'w') as launcher_file:
            #         for line in launcher_script.readlines():
            #             launcher_file.write(line)
            # for line in launcher_commands:
            #     list(execute_command(ssh, ':', f"echo '{line}\n' >> {os.environ.get('LAUNCHER_SCRIPT_NAME')}", work_dir))
    else:
        if 'input' in options:
            kind = options['input']['kind']
            options['input']['path'] = 'input' if (
                        kind == InputKind.DIRECTORY or kind == InputKind.FILES) else f"input/{options['input']['path'].rpartition('/')[2]}"

        # TODO support for various schedulers
        options['jobqueue'] = {'slurm': options['jobqueue']}

        # upload the config file for the CLI
        logger.info(f"Uploading config for task {task.guid}")
        with ssh.client.open_sftp() as sftp:
            sftp.chdir(work_dir)
            with sftp.open(f"{task.guid}.yaml", 'w') as cli_file:
                yaml.dump(del_none(options), cli_file, default_flow_style=False)


def execute_local_task(task: Task, ssh: SSH):
    precommand = '; '.join(str(task.agent.pre_commands).splitlines()) if task.agent.pre_commands else ':'
    command = f"chmod +x {task.guid}.sh && ./{task.guid}.sh"
    workdir = join(task.agent.workdir, task.workdir)

    count = 0
    lines = []
    for line in execute_command(ssh=ssh, precommand=precommand, command=command, directory=workdir, allow_stderr=True):
        stripped = line.strip()
        if count < 4 and stripped:  # TODO make the chunking size configurable
            lines.append(stripped)
            count += 1
        else:
            for l in lines: logger.info(f"[{task.agent.name}] {l}")
            # log_task_orchestrator_status(task, [f"[{task.agent.name}] {line}" for line in lines])
            lines = []
            count = 0

    task.status = TaskStatus.SUCCESS
    now = timezone.now()
    task.updated = now
    task.completed = now
    task.save()


def submit_jobqueue_task(task: Task, ssh: SSH) -> str:
    precommand = '; '.join(str(task.agent.pre_commands).splitlines()) if task.agent.pre_commands else ':'
    command = f"sbatch {task.guid}.sh"
    workdir = join(task.agent.workdir, task.workdir)

    lines = []
    for line in execute_command(ssh=ssh, precommand=precommand, command=command, directory=workdir, allow_stderr=True):
        stripped = line.strip()
        if stripped:
            logger.info(f"[{task.agent.name}] {stripped}")
            # log_task_orchestrator_status(task, [f"[{task.agent.name}] {stripped}"])
            lines.append(stripped)

    job_id = parse_task_job_id(lines[-1])
    task.job_id = job_id
    task.updated = timezone.now()
    task.save()

    logger.info(f"Set task {task.guid} job ID: {task.job_id}")
    return job_id


def log_task_orchestrator_status(task: Task, messages: List[str]):
    log_path = get_task_orchestrator_log_file_path(task)
    with open(log_path, 'a') as log:
        for message in messages:
            logger.info(f"[Task {task.guid} ({task.user.username}/{task.name})] {message}")
            log.write(f"{message}\n")


async def push_task_event(task: Task):
    user = await get_task_user(task)
    await get_channel_layer().group_send(f"{user.username}", {
        'type': 'task_event',
        'task': await sync_to_async(task_to_dict)(task),
    })


def cancel_task(task: Task, auth):
    ssh = get_task_ssh_client(task, auth)
    with ssh:
        if task.agent.executor != AgentExecutor.LOCAL:
            lines = []
            for line in execute_command(
                    ssh=ssh,
                    precommand=':',
                    command=f"squeue -u {task.agent.username}",
                    directory=join(task.agent.workdir, task.workdir),
                    allow_stderr=True):
                logger.info(line)
                lines.append(line)

            if task.job_id is None or not any([task.job_id in r for r in lines]):
                return  # run doesn't exist, so no need to cancel

            # TODO support PBS/other scheduler cancellation commands, not just SLURM
            execute_command(
                ssh=ssh,
                precommand=':',
                command=f"scancel {task.job_id}",
                directory=join(task.agent.workdir, task.workdir))


def get_task_orchestrator_log_file_name(task: Task):
    return f"plantit.{task.guid}.log"


def get_task_orchestrator_log_file_path(task: Task):
    return join(os.environ.get('TASKS_LOGS'), get_task_orchestrator_log_file_name(task))


def get_task_agent_log_file_name(task: Task):
    return f"{task.guid}.{task.agent.name.lower()}.log"


def get_task_agent_log_file_path(task: Task):
    return join(os.environ.get('TASKS_LOGS'), get_task_agent_log_file_name(task))


def get_task_scheduler_log_file_name(task: Task):
    return f"plantit.{task.job_id}.out"


def get_task_scheduler_log_file_path(task: Task):
    return join(os.environ.get('TASKS_LOGS'), get_task_scheduler_log_file_name(task))


def get_remote_logs(log_file_name: str, log_file_path: str, task: Task, ssh: SSH, sftp):
    work_dir = join(task.agent.workdir, task.workdir)
    log_path = join(work_dir, log_file_name)
    # cmd = f"test -e {log_path} && echo exists"
    # logger.info(f"Using command: {cmd}")
    # stdin, stdout, stderr = ssh.client.exec_command(cmd)
    # if not stdout.read().decode().strip() == 'exists':
    #     logger.warning(f"Agent log file {log_file_name} does not exist")
    try:
        with open(log_file_path, 'a+') as log_file:
            sftp.chdir(work_dir)
            sftp.get(log_file_name, log_file.name)
    except:
        logger.warning(f"Agent log file {log_file_name} does not exist")

    # obfuscate Docker auth info before returning logs to the user
    docker_username = environ.get('DOCKER_USERNAME', None)
    docker_password = environ.get('DOCKER_PASSWORD', None)
    lines = 0
    for line in fileinput.input([log_file_path], inplace=True):
        if docker_username in line.strip():
            line = line.strip().replace(docker_username, '*' * 7, 1)
        if docker_password in line.strip():
            line = line.strip().replace(docker_password, '*' * 7)
        lines += 1
        sys.stdout.write(line)

    logger.info(f"Retrieved {lines} line(s) from {log_file_name}")


def get_task_remote_logs(task: Task, ssh: SSH):
    with ssh:
        with ssh.client.open_sftp() as sftp:
            # orchestrator_log_file_name = get_task_orchestrator_log_file_name(task)
            # orchestrator_log_file_path = get_task_orchestrator_log_file_path(task)
            # get_remote_logs(orchestrator_log_file_name, orchestrator_log_file_path, task, ssh, sftp)

            scheduler_log_file_name = get_task_scheduler_log_file_name(task)
            scheduler_log_file_path = get_task_scheduler_log_file_path(task)
            get_remote_logs(scheduler_log_file_name, scheduler_log_file_path, task, ssh, sftp)

            if not task.agent.launcher:
                agent_log_file_name = get_task_agent_log_file_name(task)
                agent_log_file_path = get_task_agent_log_file_path(task)
                get_remote_logs(agent_log_file_name, agent_log_file_path, task, ssh, sftp)


def get_included_by_name(task: Task) -> List[str]:
    included_by_name = (
        (task.workflow['output']['include']['names'] if 'names' in task.workflow['output'][
            'include'] else [])) if 'output' in task.workflow else []
    included_by_name.append(f"{task.guid}.zip")  # zip file
    if not task.agent.launcher: included_by_name.append(f"{task.guid}.{task.agent.name.lower()}.log")
    if task.agent.executor != AgentExecutor.LOCAL and task.job_id is not None and task.job_id != '':
        included_by_name.append(f"plantit.{task.job_id}.out")
        included_by_name.append(f"plantit.{task.job_id}.err")

    return included_by_name


def get_included_by_pattern(task: Task) -> List[str]:
    included_by_pattern = (task.workflow['output']['include']['patterns'] if 'patterns' in task.workflow['output'][
        'include'] else []) if 'output' in task.workflow else []
    included_by_pattern.append('.out')
    included_by_pattern.append('.err')
    included_by_pattern.append('.zip')

    return included_by_pattern


def check_logs_for_progress(task: Task):
    """
    Parse scheduler log files for CLI output and update progress counters

    Args:
        task: The task
    """

    scheduler_log_file_path = get_task_scheduler_log_file_path(task)
    if not Path(scheduler_log_file_path).is_file():
        logger.warning(f"Scheduler log file {get_task_scheduler_log_file_name(task)} does not exist yet")
        return

    with open(scheduler_log_file_path, 'r') as scheduler_log_file:
        lines = scheduler_log_file.readlines()
        all_lines = '\n'.join(lines)

        task.inputs_downloaded = all_lines.count('Downloading file')
        task.results_transferred = all_lines.count('Uploading file')

        if task.agent.launcher:
            task.inputs_submitted = all_lines.count('running job')
            task.inputs_completed = all_lines.count('done. Exiting')
        else:
            task.inputs_submitted = all_lines.count('Submitting container')
            task.inputs_completed = all_lines.count('Container completed')

        task.save()


def remove_task_orchestration_logs(task: Task):
    log_path = get_task_orchestrator_log_file_path(task)
    os.remove(log_path)


def get_jobqueue_task_job_walltime(task: Task, auth: dict) -> (str, str):
    ssh = get_task_ssh_client(task, auth)
    with ssh:
        lines = execute_command(
            ssh=ssh,
            precommand=":",
            command=f"squeue --user={task.agent.username}",
            directory=join(task.agent.workdir, task.workdir),
            allow_stderr=True)

        try:
            job_line = next(l for l in lines if task.job_id in l)
            job_split = job_line.split()
            job_walltime = job_split[-3]
            return job_walltime
        except StopIteration:
            return None


def get_jobqueue_task_job_status(task: Task, auth: dict) -> str:
    ssh = get_task_ssh_client(task, auth)
    with ssh:
        lines = execute_command(
            ssh=ssh,
            precommand=':',
            command=f"sacct -j {task.job_id}",
            directory=join(task.agent.workdir, task.workdir),
            allow_stderr=True)

        line = next(l for l in lines if task.job_id in l)
        status = line.split()[5].replace('+', '')

        # check the scheduler log file in case `sacct` is no longer displaying info
        # about this job so we don't miss a cancellation/timeout/failure/completion
        with ssh.client.open_sftp() as sftp:

            log_file_path = get_task_scheduler_log_file_path(task)
            stdin, stdout, stderr = ssh.client.exec_command(f"test -e {log_file_path} && echo exists")
            if stdout.read().decode().strip() != 'exists': return status

            with sftp.open(log_file_path, 'r') as log_file:
                logger.info(f"Checking scheduler log file {log_file_path} for job {task.job_id} status")
                for line in log_file.readlines():
                    if 'CANCELLED' in line or 'CANCELED' in line:
                        status = 'CANCELED'
                        continue
                    if 'TIMEOUT' in line:
                        status = 'TIMEOUT'
                        continue
                    if 'FAILED' in line or 'FAILURE' in line or 'NODE_FAIL' in line:
                        status = 'FAILED'
                        break
                    if 'SUCCESS' in line or 'COMPLETED' in line:
                        status = 'SUCCESS'
                        break

                return status


def get_task_result_files(task: Task, workflow: dict, auth: dict) -> List[dict]:
    """
    Lists result files expected to be produced by the given task (assumes the task has completed). Returns a dict with form `{'name': <name>, 'path': <full path>, 'exists': <True or False>}`

    Args:
        task: The task
        workflow: The task's workflow
        auth: Authentication details for the task's agent

    Returns: Files expected to be produced by the task

    """

    # TODO factor out into method
    included_by_name = ((workflow['output']['include']['names'] if 'names' in workflow['output'][
        'include'] else [])) if 'output' in workflow else []  # [f"{run.task_id}.zip"]
    included_by_name.append(f"{task.guid}.zip")  # zip file
    if not task.agent.launcher:
        included_by_name.append(f"{task.guid}.{task.agent.name.lower()}.log")
    if task.agent.executor != AgentExecutor.LOCAL and task.job_id is not None and task.job_id != '':
        included_by_name.append(f"plantit.{task.job_id}.out")
        included_by_name.append(f"plantit.{task.job_id}.err")
    included_by_pattern = (
        workflow['output']['include']['patterns'] if 'patterns' in workflow['output'][
            'include'] else []) if 'output' in workflow else []

    ssh = get_task_ssh_client(task, auth)
    workdir = join(task.agent.workdir, task.workdir)
    outputs = []
    seen = []

    with ssh:
        with ssh.client.open_sftp() as sftp:
            for file in included_by_name:
                file_path = join(workdir, file)
                stdin, stdout, stderr = ssh.client.exec_command(f"test -e {file_path} && echo exists")
                output = {
                    'name': file,
                    'path': join(workdir, file),
                    'exists': stdout.read().decode().strip() == 'exists'
                }
                seen.append(output['name'])
                outputs.append(output)

            logger.info(f"Looking for files by pattern(s): {', '.join(included_by_pattern)}")

            for f in sftp.listdir(workdir):
                if any(pattern in f for pattern in included_by_pattern):
                    if not any(s == f for s in seen):
                        outputs.append({
                            'name': f,
                            'path': join(workdir, f),
                            'exists': True
                        })

    logger.info(f"Expecting {len(outputs)} result files for task {task.guid}: {', '.join([o['name'] for o in outputs])}")
    return outputs


def list_task_input_files(task: Task, options: PlantITCLIOptions) -> List[str]:
    input_files = terrain.list_dir(options['input']['path'], task.user.profile.cyverse_access_token)
    msg = f"Found {len(input_files)} input file(s)"
    log_task_orchestrator_status(task, [msg])
    async_to_sync(push_task_event)(task)
    logger.info(msg)

    return input_files


def get_task_ssh_client(task: Task, auth: dict) -> SSH:
    username = auth['username']
    if 'password' in auth:
        logger.info(f"Using password authentication (username: {username})")
        client = SSH(host=task.agent.hostname, port=task.agent.port, username=username, password=auth['password'])
    elif 'path' in auth:
        logger.info(f"Using key authentication (username: {username})")
        client = SSH(host=task.agent.hostname, port=task.agent.port, username=task.agent.username, pkey=auth['path'])
    else:
        raise ValueError(f"Unrecognized authentication strategy")

    return client


def parse_task_auth_options(task: Task, auth: dict) -> dict:
    if 'password' in auth:
        return PasswordTaskAuth(username=auth['username'], password=auth['password'])
    else:
        # use the agent owner's key credentials
        # (assuming that if we're already here,
        # submitter has valid access to agent)
        return KeyTaskAuth(username=auth['username'], path=str(get_user_private_key_path(task.agent.user.username)))


def get_agent_log_file_contents(task: Task) -> List[str]:
    agent_log_file_path = get_task_agent_log_file_path(task)
    if not task.agent.launcher and Path(agent_log_file_path).is_file():
        with open(agent_log_file_path, 'r') as log:
            agent_logs = [line.strip() for line in log.readlines()[-int(1000000):]]
    else:
        agent_logs = []

    return agent_logs


def get_scheduler_log_file_contents(task: Task) -> List[str]:
    scheduler_log_file_path = get_task_scheduler_log_file_path(task)
    if Path(scheduler_log_file_path).is_file():
        with open(scheduler_log_file_path, 'r') as log:
            scheduler_logs = [line.strip() + "\n" for line in log.readlines()[-int(1000000):]]
    else:
        scheduler_logs = []

    return scheduler_logs


def should_transfer_results(task: Task) -> bool:
    return 'output' in task.workflow['config'] and 'to' in task.workflow['config']['output']


def task_to_dict(task: Task) -> dict:
    orchestrator_log_file_path = get_task_orchestrator_log_file_path(task)
    if Path(orchestrator_log_file_path).is_file():
        with open(orchestrator_log_file_path, 'r') as log:
            orchestrator_logs = [line.strip() for line in log.readlines()[-int(1000000):]]
    else: orchestrator_logs = []

    # try:
    #     AgentAccessPolicy.objects.get(user=task.user, agent=task.agent, role__in=[AgentRole.admin, AgentRole.guest])
    #     can_restart = True
    # except:
    #     can_restart = False

    results = RedisClient.get().get(f"results/{task.guid}")

    return {
        # 'can_restart': can_restart,
        'guid': task.guid,
        'status': task.status,
        'owner': task.user.username,
        'name': task.name,
        'project': {
            'title': task.project.title,
            'owner': task.project.owner.username,
            'description': task.project.description
        } if task.project is not None else None,
        'study': {
            'title': task.study.title,
            'description': task.study.description
        } if task.study is not None else None,
        'work_dir': task.workdir,
        'orchestrator_logs': orchestrator_logs,
        'inputs_detected': task.inputs_detected,
        'inputs_downloaded': task.inputs_downloaded,
        'inputs_submitted': task.inputs_submitted,
        'inputs_completed': task.inputs_completed,
        'agent': agent_to_dict(task.agent) if task.agent is not None else None,
        'created': task.created.isoformat(),
        'updated': task.updated.isoformat(),
        'completed': task.completed.isoformat() if task.completed is not None else None,
        'due_time': None if task.due_time is None else task.due_time.isoformat(),
        'cleanup_time': None if task.cleanup_time is None else task.cleanup_time.isoformat(),
        'workflow_owner': task.workflow_owner,
        'workflow_name': task.workflow_name,
        'workflow_branch': task.workflow_branch,
        'workflow_image_url': task.workflow_image_url,
        'input_path': task.workflow['config']['input']['path'] if 'input' in task.workflow['config'] else None,
        'output_path': task.workflow['config']['output']['to'] if (
                    'output' in task.workflow['config'] and 'to' in task.workflow['config']['output']) else None,
        'tags': [str(tag) for tag in task.tags.all()],
        'is_complete': task.is_complete,
        'is_success': task.is_success,
        'is_failure': task.is_failure,
        'is_cancelled': task.is_cancelled,
        'is_timeout': task.is_timeout,
        'result_previews_loaded': task.previews_loaded,
        'result_transfer': should_transfer_results(task),
        'results_retrieved': task.results_retrieved,
        'results_transferred': task.results_transferred,
        'cleaned_up': task.cleaned_up,
        'transferred': task.transferred,
        'transfer_path': task.transfer_path,
        'output_files': json.loads(results) if results is not None else [],
        'job_id': task.job_id,
        'job_status': task.job_status,
        'job_walltime': task.job_consumed_walltime,
        'delayed_id': task.delayed_id,
        'repeating_id': task.repeating_id
    }


def delayed_task_to_dict(task: DelayedTask) -> dict:
    return {
        # 'agent': agent_to_dict(task.agent),
        'name': task.name,
        'eta': task.eta,
        'enabled': task.enabled,
        'interval': {
            'every': task.interval.every,
            'period': task.interval.period
        },
        'last_run': task.last_run_at,
        'workflow_owner': task.workflow_owner,
        'workflow_name': task.workflow_name,
        'workflow_branch': task.workflow_branch,
        'workflow_image_url': task.workflow_image_url,

    }


def repeating_task_to_dict(task: RepeatingTask):
    return {
        # 'agent': agent_to_dict(task.agent),
        'name': task.name,
        'eta': task.eta,
        'interval': {
            'every': task.interval.every,
            'period': task.interval.period
        },
        'enabled': task.enabled,
        'last_run': task.last_run_at,
        'workflow_owner': task.workflow_owner,
        'workflow_name': task.workflow_name,
        'workflow_branch': task.workflow_branch,
        'workflow_image_url': task.workflow_image_url,
    }


def create_immediate_task(user: User, workflow):
    repo_owner = workflow['repo']['owner']['login']
    repo_name = workflow['repo']['name']
    repo_branch = workflow['branch']['name']

    redis = RedisClient.get()
    last_config = workflow.copy()
    del last_config['auth']
    last_config['timestamp'] = timezone.now().isoformat()
    redis.set(f"workflow_configs/{user.username}/{repo_owner}/{repo_name}/{repo_branch}", json.dumps(last_config))

    config = workflow['config']
    branch = workflow['branch']
    task_name = config.get('task_name', None)
    task_guid = config.get('task_guid', None) if workflow['type'] != 'Every' else str(uuid.uuid4())

    # TODO handle repeating tasks & repeating GUID issue

    agent = Agent.objects.get(name=config['agent']['name'])
    task = create_task(
        username=user.username,
        agent_name=agent.name,
        workflow=workflow,
        branch=branch,
        name=task_name if task_name is not None and task_name != '' else task_guid,
        guid=task_guid,
        project=workflow['miappe']['project']['title'] if workflow['miappe']['project'] is not None else None,
        study=workflow['miappe']['study']['title'] if workflow['miappe']['study'] is not None else None)

    return task


def create_delayed_task(user: User, workflow):
    now = timezone.now().timestamp()
    id = f"{user.username}-delayed-{now}"
    eta, seconds = parse_task_eta(workflow)
    schedule, _ = IntervalSchedule.objects.get_or_create(every=seconds, period=IntervalSchedule.SECONDS)

    repo_owner = workflow['repo']['owner']['login']
    repo_name = workflow['repo']['name']
    repo_branch = workflow['branch']['name']

    if 'logo' in workflow['config']:
        logo_path = workflow['config']['logo']
        workflow_image_url = f"https://raw.githubusercontent.com/{repo_owner}/{repo_name}/{repo_branch}/{logo_path}"
    else: workflow_image_url = None

    task, created = DelayedTask.objects.get_or_create(
        user=user,
        interval=schedule,
        eta=eta,
        one_off=True,
        workflow_owner=repo_owner,
        workflow_name=repo_name,
        workflow_branch=repo_branch,
        workflow_image_url=workflow_image_url,
        name=id,
        task='plantit.celery_tasks.create_and_submit_delayed',
        args=json.dumps([user.username, workflow, id]))

    # manually refresh task schedule
    PeriodicTasks.changed(task)

    return task, created


def create_repeating_task(user: User, workflow):
    now = timezone.now().timestamp()
    id = f"{user.username}-repeating-{now}"
    eta, seconds = parse_task_eta(workflow)
    schedule, _ = IntervalSchedule.objects.get_or_create(every=seconds, period=IntervalSchedule.SECONDS)

    repo_owner = workflow['repo']['owner']['login']
    repo_name = workflow['repo']['name']
    repo_branch = workflow['branch']['name']

    if 'logo' in workflow['config']:
        logo_path = workflow['config']['logo']
        workflow_image_url = f"https://raw.githubusercontent.com/{repo_owner}/{repo_name}/{repo_branch}/{logo_path}"
    else: workflow_image_url = None

    task, created = RepeatingTask.objects.get_or_create(
        user=user,
        interval=schedule,
        eta=eta,
        workflow_owner=repo_owner,
        workflow_name=repo_name,
        workflow_branch=repo_branch,
        workflow_image_url=workflow_image_url,
        name=id,
        task='plantit.celery_tasks.create_and_submit_repeating',
        args=json.dumps([user.username, workflow, id]))

    # manually refresh task schedule
    PeriodicTasks.changed(task)

    return task, created


# notifications

def notification_to_dict(notification: Notification) -> dict:
    return {
        'id': notification.guid,
        'username': notification.user.username,
        'created': notification.created.isoformat(),
        'message': notification.message,
        'read': notification.read,
    }


# datasets

def dataset_access_policy_to_dict(policy: DatasetAccessPolicy):
    return {
        'owner': policy.owner.username,
        'guest': policy.guest.username,
        'path': policy.path,
        'role': policy.role.value
    }


# agents

@sync_to_async
def get_agent_user(agent: Agent):
    return agent.user


@sync_to_async
def agent_to_dict_async(agent: Agent, user: User = None):
    return agent_to_dict(agent, user)


def agent_to_dict(agent: Agent, user: User = None) -> dict:
    # tasks = AgentTask.objects.filter(agent=agent)
    users_authorized = agent.users_authorized.all() if agent.users_authorized is not None else []
    mapped = {
        'name': agent.name,
        'guid': agent.guid,
        'role': AgentRole.admin if user is not None and agent.user == user else AgentRole.guest,
        'description': agent.description,
        'hostname': agent.hostname,
        'username': agent.username,
        'pre_commands': agent.pre_commands,
        'max_walltime': agent.max_walltime,
        'max_mem': agent.max_mem,
        'max_cores': agent.max_cores,
        'max_processes': agent.max_processes,
        'queue': agent.queue,
        'project': agent.project,
        'workdir': agent.workdir,
        'executor': agent.executor,
        'disabled': agent.disabled,
        'public': agent.public,
        'gpus': agent.gpus,
        # 'tasks': [agent_task_to_dict(task) for task in tasks],
        'logo': agent.logo,
        'is_local': agent.executor == AgentExecutor.LOCAL,
        'is_healthy': agent.is_healthy,
        'users_authorized': [get_user_bundle(user) for user in users_authorized if user is not None],
    }

    if agent.user is not None: mapped['user'] = agent.user.username
    return mapped


def agent_task_to_dict(task: AgentTask) -> dict:
    return {
        'name': task.name,
        'description': task.description,
        'command': task.command,
        'crontab': str(task.crontab).rpartition("(")[0].strip(),
        'enabled': task.enabled,
        'last_run': task.last_run_at
    }


def has_virtual_memory(agent: Agent) -> bool:
    return agent.header_skip is not None and '--mem' in agent.header_skip


@retry(
    wait=wait_exponential(multiplier=1, min=4, max=10),
    stop=stop_after_attempt(3),
    retry=(retry_if_exception_type(ConnectionError) | retry_if_exception_type(
        RequestException) | retry_if_exception_type(ReadTimeout) | retry_if_exception_type(
        Timeout) | retry_if_exception_type(HTTPError)))
def is_healthy(agent: Agent, auth: dict) -> (bool, List[str]):
    """
    Checks agent health

    Args:
        agent: the agent
        auth: authentication info (must always include 'username' and 'port' and also 'password' if password authentication is used for this agent)

    Returns: True if the agent was successfully reached, otherwise false.
    """

    output = []
    try:
        ssh = SSH(host=agent.hostname, port=agent.port, username=agent.username, pkey=str(get_user_private_key_path(agent.user.username)))

        try:
            with ssh:
                logger.info(f"Checking agent {agent.name}'s health")
                for line in execute_command(ssh=ssh, precommand=':', command=f"pwd", directory=agent.workdir):
                    logger.info(line)
                    output.append(line)
                logger.info(f"Agent {agent.name} healthcheck succeeded")
                return True, output
        except SSHException as e:
            if 'not found in known_hosts' in str(e):
                # add the hostname to known_hosts and retry
                subprocess.run(f"ssh-keyscan {agent.hostname} >> /code/config/ssh/known_hosts", shell=True)
                with ssh:
                    logger.info(f"Checking agent {agent.name}'s health")
                    for line in execute_command(ssh=ssh, precommand=':', command=f"pwd", directory=agent.workdir):
                        logger.info(line)
                        output.append(line)
                    logger.info(f"Agent {agent.name} healthcheck succeeded")
                    return True, output
            else:
                raise e
    except:
        msg = f"Agent {agent.name} healthcheck failed:\n{traceback.format_exc()}"
        logger.warning(msg)
        output.append(msg)
        return False, output


# MIAPPE

def person_to_dict(user: User, role: str) -> dict:
    return {
        'name': f"{user.first_name} {user.last_name}",
        'email': user.email,
        'id': user.username,
        'affiliation': user.profile.institution,
        'role': role,
    }


def study_to_dict(study: Study, project: Investigation) -> dict:
    team = [person_to_dict(person, 'Researcher') for person in study.team.all()]
    return {
        'project_title': project.title,
        'project_owner': project.owner.username,
        'guid': study.guid,
        'title': study.title,
        'description': study.description,
        'start_date': study.start_date,
        'end_date': study.end_date,
        'contact_institution': study.contact_institution,
        'country': study.country,
        'site_name': study.site_name if study.site_name != '' else None,
        'latitude': study.latitude,
        'longitude': study.longitude,
        'altitude': study.altitude,
        'altitude_units': study.altitude_units,
        'experimental_design_description': study.experimental_design_description if study.experimental_design_description != '' else None,
        'experimental_design_type': study.experimental_design_type if study.experimental_design_type != '' else None,
        'experimental_design_map': study.experimental_design_map if study.experimental_design_map != '' else None,
        'observation_unit_level_hierarchy': study.observation_unit_level_hierarchy if study.observation_unit_level_hierarchy != '' else None,
        'observation_unit_description': study.observation_unit_description if study.observation_unit_description != '' else None,
        'growth_facility_description': study.growth_facility_description if study.growth_facility_description != '' else None,
        'growth_facility_type': study.growth_facility_type if study.growth_facility_type != '' else None,
        'cultural_practices': study.cultural_practices if study.cultural_practices != '' else None,
        'team': team,
        'dataset_paths': study.dataset_paths if study.dataset_paths is not None else []
    }


def get_project_workflows(project: Investigation):
    redis = RedisClient.get()
    workflows = [wf for wf in [json.loads(redis.get(key)) for key in redis.scan_iter(match='workflows/*')] if
                 'projects' in wf['config'] and project.guid in wf['config']['projects']]
    return workflows


def project_to_dict(project: Investigation) -> dict:
    studies = [study_to_dict(study, project) for study in Study.objects.filter(investigation=project)]
    team = [person_to_dict(person, 'Researcher') for person in project.team.all()]
    return {
        'guid': project.guid,
        'owner': project.owner.username,
        'title': project.title,
        'description': project.description,
        'submission_date': project.submission_date,
        'public_release_date': project.public_release_date,
        'associated_publication': project.associated_publication,
        'studies': studies,
        'team': team,
        'workflows': get_project_workflows(project)
    }


@sync_to_async
def check_user_authentication(user):
    return user.is_authenticated


# updates

def update_to_dict(update: NewsUpdate):
    return {
        'created': update.created.isoformat(),
        'content': update.content
    }
