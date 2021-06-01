from django.conf import settings
from django.conf.urls import url, include
from django.conf.urls.static import static
from django.contrib.staticfiles.storage import staticfiles_storage
from django.http import HttpResponseRedirect
from django.views.generic import RedirectView
from rest_framework import routers

from django.urls import path

from .auth.views import login_view, logout_view
from .datasets.consumers import DatasetSessionConsumer
from .miappe.views import *
from .agents.views import AgentsViewSet
from .users.views import UsersViewSet, IDPViewSet
from .runs.consumers import RunConsumer
from .notifications.consumers import NotificationConsumer
from .workflows.consumers import WorkflowConsumer

router = routers.DefaultRouter()
router.register('users', UsersViewSet)
router.register('idp', IDPViewSet, basename='idp')
router.register('agents', AgentsViewSet)
router.register('miappe/investigations', InvestigationViewSet)
router.register('miappe/studies', StudyViewSet)
router.register('miappe/roles', RoleViewSet)
router.register('miappe/files', FileViewSet)
router.register('miappe/biological_materials', BiologicalMaterialViewSet)
router.register('miappe/environment_parameters', EnvironmentParameterViewSet)
router.register('miappe/experimental_factors', ExperimentalFactorViewSet)
router.register('miappe/events', EventViewSet)
router.register('miappe/observation_units', ObservationUnitViewSet)
router.register('miappe/samples', SampleViewSet)
router.register('miappe/observed_variables', ObservedVariableViewSet)

urlpatterns = [
                  url('', include(router.urls)),
                  url('auth/login/', login_view),
                  url('auth/logout/', logout_view),
                  url('datasets/', include("plantit.datasets.urls")),
                  url('workflows/', include("plantit.workflows.urls")),
                  url('runs/', include("plantit.runs.urls")),
                  url('stats/', include("plantit.stats.urls")),
                  url('notifications/', include("plantit.notifications.urls")),
                  # url('favicon.ico', RedirectView.as_view(url=staticfiles_storage.url('favicon.ico'))),
              ] + static(r'/favicon.ico', document_root='static/favicon.ico')

websocket_urlpatterns = [
    path(r'ws/runs/<username>/', RunConsumer.as_asgi()),
    path(r'ws/workflows/<owner>/', WorkflowConsumer.as_asgi()),
    path(r'ws/notifications/<username>/', NotificationConsumer.as_asgi()),
    path(r'ws/sessions/<guid>/', DatasetSessionConsumer.as_asgi())
]
