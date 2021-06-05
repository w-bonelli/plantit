import json
import logging
from os.path import join
from typing import List

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.contrib.auth.models import User
from django.utils import timezone

from plantit.github import get_repo, list_connectable_repos_by_owner
from plantit.redis import RedisClient
from plantit.tasks.models import Task
from plantit.workflows.models import Workflow

logger = logging.getLogger(__name__)


def map_old_workflow_config_to_new(old: dict, run: Task, resources: dict) -> dict:
    new_config = {
        'image': old['config']['image'],
        'command': old['config']['commands'],
        'workdir': old['config']['workdir'],
        'log_file': f"{run.guid}.{run.agent.name.lower()}.log"
    }

    del old['config']['agent']

    if 'mount' in old['config']:
        new_config['bind_mounts'] = old['config']['mount']

    if 'parameters' in old['config']:
        old_params = old['config']['parameters']
        params = []
        for p in old_params:
            if p['type'] == 'string':
                params.append({
                    'key': p['name'],
                    'value': str(p['value'])
                })
            elif p['type'] == 'select':
                params.append({
                    'key': p['name'],
                    'value': str(p['value'])
                })
            elif p['type'] == 'number':
                params.append({
                    'key': p['name'],
                    'value': str(p['value'])
                })
            elif p['type'] == 'boolean':
                params.append({
                    'key': p['name'],
                    'value': str(p['value'])
                })
        new_config['parameters'] = params

    if 'input' in old['config']:
        input_kind = old['config']['input']['kind'] if 'kind' in old['config']['input'] else None
        new_config['input'] = dict()
        if input_kind == 'directory':
            new_config['input']['directory'] = dict()
            new_config['input']['directory']['path'] = join(run.agent.workdir, run.workdir, 'input')
            new_config['input']['directory']['patterns'] = old['config']['input']['patterns']
        elif input_kind == 'files':
            new_config['input']['files'] = dict()
            new_config['input']['files']['path'] = join(run.agent.workdir, run.workdir, 'input')
            new_config['input']['files']['patterns'] = old['config']['input']['patterns']
        elif input_kind == 'file':
            new_config['input']['file'] = dict()
            new_config['input']['file']['path'] = join(run.agent.workdir, run.workdir, 'input',
                                                       old['config']['input']['from'].rpartition('/')[2])

    sandbox = run.agent.name == 'Sandbox'
    work_dir = join(run.agent.workdir, run.workdir)
    if not sandbox and not run.agent.job_array:
        new_config['jobqueue'] = dict()
        new_config['jobqueue']['slurm'] = {
            'cores': resources['cores'],
            'processes': resources['processes'],
            'walltime': resources['time'],
            'local_directory': work_dir,
            'log_directory': work_dir,
            'env_extra': [run.agent.pre_commands]
        }

        if 'mem' in resources:
            new_config['jobqueue']['slurm']['memory'] = resources['mem']
        if run.agent.queue is not None and run.agent.queue != '':
            new_config['jobqueue']['slurm']['queue'] = run.agent.queue
        if run.agent.project is not None and run.agent.project != '':
            new_config['jobqueue']['slurm']['project'] = run.agent.project
        if run.agent.header_skip is not None and run.agent.header_skip != '':
            new_config['jobqueue']['slurm']['header_skip'] = run.agent.header_skip.split(',')

        if 'gpu' in old['config'] and old['config']['gpu']:
            if run.agent.gpu:
                print(f"Using GPU on {run.agent.name} queue '{run.agent.gpu_queue}'")
                new_config['gpu'] = True
                new_config['jobqueue']['slurm']['job_extra'] = [f"--gres=gpu:1"]
                new_config['jobqueue']['slurm']['queue'] = run.agent.gpu_queue
            else:
                print(f"No GPU support on {run.agent.name}")

    return new_config


def map_workflow(workflow: Workflow, token: str) -> dict:
    repo = get_repo(
        workflow.repo_owner,
        workflow.repo_name,
        token)
    return {
        'config': repo['config'],
        'repo': repo['repo'],
        'validation': repo['validation'],
        'public': workflow.public,
        'connected': True
    }


def populate_workflow_cache(owner: str, workflows: List[dict]):
    redis = RedisClient.get()
    for workflow in workflows:
        redis.set(f"workflows/{owner}/{workflow['repo']['name']}", json.dumps(workflow))
        async_to_sync(get_channel_layer().group_send)(f"workflows-{owner}", {
            'type': 'update_workflow',
            'workflow': workflow
        })

    redis.set(f"workflows_updated/{owner}", timezone.now().timestamp())


def clean_workflow_cache(owner: str):
    redis = RedisClient.get()
    deleted = 0
    keys = list(redis.scan_iter(match=f"workflows/{owner}/*"))
    for key in keys:
        repo = json.loads(redis.get(key))
        deleted += 1
        redis.delete(key)
        async_to_sync(get_channel_layer().group_send)(f"workflows-{owner}", {
            'type': 'remove_workflow',
            'workflow': repo
        })

    return len(keys)


def rescan_personal_workflows(owner: str):
    try:
        user = User.objects.get(profile__github_username=owner)
    except:
        logger.warning(f"User {owner} does not exist")
        return

    cleaned = clean_workflow_cache(owner)
    logger.info(f"Cleaned {cleaned} stale workflow(s) from {owner}'s cache ")

    both = []
    connectable = list_connectable_repos_by_owner(owner, user.profile.github_token)
    connected = [map_workflow(workflow, user.profile.github_token) for workflow in list(Workflow.objects.filter(user=user))]

    for able in connectable:
        if not any(['name' in ed['config'] and 'name' in able['config'] and ed['config']['name'] == able['config']['name'] for ed in connected]):
            able['public'] = False
            able['connected'] = False
            both.append(able)

    missing = 0
    for ed in connected:
        name = ed['config']['name']
        if not any(['name' in able['config'] and able['config']['name'] == name for able in connectable]):
            missing += 1
            logger.warning(f"Configuration file missing for {owner}'s workflow {name}")
            ed['validation'] = {
                'is_valid': False,
                'errors': ["Configuration file missing"]
            }
        both.append(ed)

    populate_workflow_cache(owner, both)
    logger.info(
        f"Found {len(connected)} connected workflow(s), {len(connectable) - len(connected)} connectable, wrote {len(both)} to {owner}'s cache" + "" if missing == 0 else f"({missing} with missing configuration files)")


def rescan_public_workflows(token: str):
    redis = RedisClient.get()
    public = Workflow.objects.filter(public=True)
    private = Workflow.objects.filter(public=False)

    for workflow in public:
        repo = get_repo(workflow.repo_owner, workflow.repo_name, token)
        repo['public'] = True
        redis.set(f"workflows/{workflow.repo_owner}/{workflow.repo_name}", json.dumps(repo))

    for workflow in private:
        repo = get_repo(workflow.repo_owner, workflow.repo_name, token)
        repo['public'] = False
        redis.set(f"workflows/{workflow.repo_owner}/{workflow.repo_name}", json.dumps(repo))

    logger.info(f"Refreshed public workflows ({len(public)})")
    redis.set(f"public_workflows_updated", timezone.now().timestamp())
