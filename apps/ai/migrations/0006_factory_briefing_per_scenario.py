# Generated manually for per-scenario factory briefings.

import django.db.models.deletion
from django.db import migrations, models


def forwards_assign_basecase_scenario(apps, schema_editor):
    FactoryBriefing = apps.get_model("ai", "FactoryBriefing")
    Scenario = apps.get_model("rmct", "Scenario")

    for row in FactoryBriefing.objects.all():
        basecase = (
            Scenario.objects.filter(model_id=row.factory_id, is_basecase=True)
            .order_by("created_at")
            .first()
        )
        if basecase is None:
            row.delete()
            continue
        row.scenario_id = basecase.id
        row.save(update_fields=["scenario_id"])


def backwards_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0005_factory_briefing"),
        ("rmct", "0006_rmcmmodel_last_updated_by_name_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="factorybriefing",
            name="scenario",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="ai_factory_briefings",
                to="rmct.scenario",
            ),
        ),
        migrations.AlterField(
            model_name="factorybriefing",
            name="factory",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="ai_factory_briefings",
                to="rmct.rmcmmodel",
            ),
        ),
        migrations.RunPython(forwards_assign_basecase_scenario, backwards_noop),
        migrations.AlterField(
            model_name="factorybriefing",
            name="scenario",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="ai_factory_briefings",
                to="rmct.scenario",
            ),
        ),
        migrations.AddConstraint(
            model_name="factorybriefing",
            constraint=models.UniqueConstraint(
                fields=("factory", "scenario"),
                name="ai_factorybriefing_factory_scenario_uniq",
            ),
        ),
    ]
