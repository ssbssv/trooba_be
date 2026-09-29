from django.urls import path
from . import views


urlpatterns = [
    path(
        "models/<uuid:model_id>/ibom/",
        views.model_ibom_create,
        name="model_ibom_create",
    ),
    path(
        "models/<uuid:model_id>/ibom/<str:parent_id>/",
        views.model_ibom_set_for_parent,
        name="model_ibom_set_for_parent",
    ),
    path(
        "models/<uuid:model_id>/ibom/entry/<str:entry_id>/",
        views.model_ibom_update,
        name="model_ibom_update",
    ),
    path(
        "models/<uuid:model_id>/ibom/entry/<str:entry_id>/delete/",
        views.model_ibom_delete,
        name="model_ibom_delete",
    ),
]

