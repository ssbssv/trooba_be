from django.urls import path
from django.views.decorators.csrf import csrf_exempt

from . import views

urlpatterns = [

    # Organization-scoped labor API
    path("api/labor", views.get_labors),
    path("api/labor/add", csrf_exempt(views.add_labor)),
    path("api/labor/<uuid:labor_id>", views.get_labor),
    path("api/labor/update/<uuid:labor_id>", csrf_exempt(views.update_labor)),
    path("api/labor/delete/<uuid:labor_id>", csrf_exempt(views.delete_labor)),

    # Model-scoped labor API used by RMCT frontend
    path("api/models/<uuid:model_id>/labor/", csrf_exempt(views.model_labor_create)),
    path("api/models/<uuid:model_id>/labor/<str:labor_id>/", csrf_exempt(views.model_labor_update)),
    path("api/models/<uuid:model_id>/labor/<str:labor_id>/delete/", csrf_exempt(views.model_labor_delete)),

]