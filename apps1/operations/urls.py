from django.urls import path
from . import views


urlpatterns = [
    path(
        "models/<uuid:model_id>/operations/",
        views.model_operations_create,
        name="model_operations_create",
    ),
    path(
        "models/<uuid:model_id>/operations/<str:op_id>/",
        views.model_operations_update,
        name="model_operations_update",
    ),
    path(
        "models/<uuid:model_id>/operations/<str:op_id>/delete/",
        views.model_operations_delete,
        name="model_operations_delete",
    ),
]

