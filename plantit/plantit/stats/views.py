from django.contrib.auth.models import User
from django.http import JsonResponse
from rest_framework.decorators import api_view

from plantit.runs.models import Run
from plantit.workflows.models import Workflow


@api_view(['GET'])
def counts(request):
    return JsonResponse({
        'users': User.objects.count(),
        'workflows': Workflow.objects.count(),
        'runs': Run.objects.count()
    })
