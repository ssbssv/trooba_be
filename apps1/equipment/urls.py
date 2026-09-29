from django.urls import path
from . import views


urlpatterns = [
    path(
        "models/<uuid:model_id>/equipment/",
        views.model_equipment_create,
        name="model_equipment_create",
    ),
    path(
        "models/<uuid:model_id>/equipment/<str:equip_id>/",
        views.model_equipment_update,
        name="model_equipment_update",
    ),
    path(
        "models/<uuid:model_id>/equipment/<str:equip_id>/delete/",
        views.model_equipment_delete,
        name="model_equipment_delete",
    ),
]

