from uuid import uuid4
from pprint import pprint

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase

import plantit.queries as q

from plantit.users.models import Profile
from plantit.tasks.models import Task
from plantit.tokens import TerrainToken


class QueriesTests(TestCase):
    def setUp(self):
        user = User.objects.create(username='wbonelli', first_name="Wes", last_name="Bonelli")
        profile = Profile.objects.create(
            user=user,
            github_username='w-bonelli',
            github_token=settings.GITHUB_TOKEN,
            cyverse_access_token=TerrainToken.get(),
            institution='University of Georgia',
            first_login=True
        )
        guid = str(uuid4())
        # task = Task.objects.create(
        #     guid=guid,
        #     name=guid,
        #     user=user,
        #     workflow=workflow,
        #     workflow_owner=repo_owner,
        #     workflow_name=repo_name,
        #     workflow_branch=repo_branch,
        #     agent=agent,
        #     status=TaskStatus.CREATED,
        #     created=now,
        #     updated=now,
        #     due_time=due_time,
        #     token=binascii.hexlify(os.urandom(20)).decode())

    def test_get_workflows_usage_timeseries(self):
        user = User.objects.get(username='wbonelli')
        series = q.get_workflows_usage_timeseries(user)
        pprint(series)

    def test_get_institutions(self):
        institutions = q.get_institutions()
        self.assertTrue('university of georgia' in institutions)
        self.assertTrue(institutions['university of georgia']['count'] == 1)
        self.assertTrue(institutions['university of georgia']['geocode']['text'] == 'University of Georgia')
