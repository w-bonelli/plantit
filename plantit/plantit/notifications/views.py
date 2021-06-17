from itertools import chain

from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import HttpResponseNotFound, JsonResponse

from plantit.notifications.models import Notification
from plantit.utils import notification_to_dict


@login_required
def list_by_user(request, owner):
    try:
        params = request.query_params
    except:
        params = {}
    page = params.get('page') if 'page' in params else 0
    start = int(page) * 20
    count = start + 20

    try: user = User.objects.get(username=owner)
    except: return HttpResponseNotFound()

    notifications = list(chain(
        # list(DirectoryPolicyNotification.objects.filter(user=user)),
        list(Notification.objects.filter(user=user))))
    notifications = notifications[start:(start + count)]

    return JsonResponse({'notifications': [notification_to_dict(notification) for notification in notifications]})


@login_required
def get_or_dismiss(request, owner, guid):
    try:
        user = User.objects.get(username=owner)
        notification = Notification.objects.get(user=user, guid=guid)
    except: return HttpResponseNotFound()

    if request.method == 'GET': return JsonResponse(notification_to_dict(notification))
    elif request.method == 'DELETE':
        notification.delete()
        notifications = Notification.objects.filter(user=user)
        return JsonResponse({'notifications': [notification_to_dict(notification) for notification in notifications]})
