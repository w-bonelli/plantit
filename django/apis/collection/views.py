from rest_framework import viewsets
from .serializers import CollectionSerializer
from plantit.collection.models import Collection
from rest_framework.permissions import IsAuthenticated

class CollectionViewSet(viewsets.ModelViewSet):
    """
    API endpoint that allows collections to be viewed and edited.
    """
    permission_classes = (IsAuthenticated,)

    queryset = Collection.objects.all()
    serializer_class = CollectionSerializer