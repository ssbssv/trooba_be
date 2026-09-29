from django.urls import path
from . import views


urlpatterns = [
    path(
        "models/<uuid:model_id>/routing/",
        views.model_routing_create,
        name="model_routing_create",
    ),
    path(
        "models/<uuid:model_id>/routing/set/",
        views.model_routing_set,
        name="model_routing_set",
    ),
    path(
        "models/<uuid:model_id>/routing/<str:route_id>/",
        views.model_routing_update,
        name="model_routing_update",
    ),
    path(
        "models/<uuid:model_id>/routing/<str:route_id>/delete/",
        views.model_routing_delete,
        name="model_routing_delete",
    ),
]

