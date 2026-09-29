from django.contrib import admin
from .models import RMCMModel, ModelVersion, Scenario, ScenarioChange, ScenarioResult


@admin.register(RMCMModel)
class RMCMModelAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'run_status', 'updated_at', 'owner')
    list_filter = ('run_status', 'is_archived', 'is_demo')
    search_fields = ('name', 'description')


@admin.register(ModelVersion)
class ModelVersionAdmin(admin.ModelAdmin):
    list_display = ('id', 'model', 'label', 'created_at')
    list_filter = ('model',)


@admin.register(Scenario)
class ScenarioAdmin(admin.ModelAdmin):
    list_display = ('id', 'model', 'name', 'is_basecase', 'status', 'updated_at')
    list_filter = ('is_basecase', 'status')


@admin.register(ScenarioChange)
class ScenarioChangeAdmin(admin.ModelAdmin):
    list_display = ('id', 'scenario', 'data_type', 'entity_name', 'field_name')


@admin.register(ScenarioResult)
class ScenarioResultAdmin(admin.ModelAdmin):
    list_display = ('scenario', 'created_at')
