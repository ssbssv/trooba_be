from django.urls import path
from . import views


urlpatterns = [
    path(
        "models/<uuid:model_id>/products/",
        views.model_products_create,
        name="model_products_create",
    ),
    path(
        "models/<uuid:model_id>/products/<str:product_id>/",
        views.model_products_update,
        name="model_products_update",
    ),
    path(
        "models/<uuid:model_id>/products/<str:product_id>/delete/",
        views.model_products_delete,
        name="model_products_delete",
    ),
    path(
        "models/<uuid:model_id>/products/<str:product_id>/operations-and-routing/",
        views.model_products_clear_ops_routing,
        name="model_products_clear_ops_routing",
    ),
]

