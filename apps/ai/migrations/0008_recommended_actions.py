# Generated manually for Recommended Actions feature

import django.db.models.deletion
import django.utils.timezone
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0007_factory_analysis"),
        ("rmct", "0006_rmcmmodel_last_updated_by_name_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="RecommendedActions",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("recommendations", models.JSONField(default=dict)),
                (
                    "result_version",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                (
                    "analysis_fingerprint",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                (
                    "prompt_version",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                (
                    "model_name",
                    models.CharField(blank=True, default="", max_length=128),
                ),
                ("source", models.CharField(blank=True, default="ai", max_length=64)),
                (
                    "generated_at",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "analysis",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="recommended_actions_rows",
                        to="ai.factoryanalysis",
                    ),
                ),
                (
                    "factory",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ai_recommended_actions",
                        to="rmct.rmcmmodel",
                    ),
                ),
                (
                    "scenario",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ai_recommended_actions",
                        to="rmct.scenario",
                    ),
                ),
            ],
            options={
                "ordering": ["-generated_at"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("factory", "scenario"),
                        name="ai_recommendedactions_factory_scenario_uniq",
                    )
                ],
            },
        ),
    ]
