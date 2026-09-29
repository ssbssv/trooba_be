# Generated manually for ChatSession expiration / model-version fields.

from django.db import migrations, models
import django.utils.timezone


def backfill_session_fields(apps, schema_editor):
    ChatSession = apps.get_model("ai", "ChatSession")
    RMCMModel = apps.get_model("rmct", "RMCMModel")
    factories = {str(f.id): f for f in RMCMModel.objects.all()}
    for session in ChatSession.objects.all().iterator():
        factory = factories.get(str(session.factory_id))
        updated = ""
        last_run = ""
        run_status = ""
        if factory is not None:
            updated = factory.updated_at.isoformat() if factory.updated_at else ""
            last_run = factory.last_run_at.isoformat() if factory.last_run_at else ""
            run_status = factory.run_status or ""
            model_version = f"{factory.id}:{updated}:{last_run}:{run_status}"
        else:
            model_version = ""
        activity = session.updated_at or session.created_at or django.utils.timezone.now()
        ChatSession.objects.filter(pk=session.pk).update(
            status="ACTIVE",
            lock_reason=None,
            model_version=model_version,
            last_activity_at=activity,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0001_initial_chat_session_message"),
    ]

    operations = [
        migrations.AddField(
            model_name="chatsession",
            name="status",
            field=models.CharField(
                choices=[("ACTIVE", "Active"), ("READ_ONLY", "Read only")],
                db_index=True,
                default="ACTIVE",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="chatsession",
            name="lock_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("SESSION_TIMEOUT", "Session timeout"),
                    ("MODEL_CHANGED", "Model changed"),
                ],
                default=None,
                max_length=32,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="chatsession",
            name="model_version",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="chatsession",
            name="last_activity_at",
            field=models.DateTimeField(
                db_index=True, default=django.utils.timezone.now
            ),
        ),
        migrations.AddIndex(
            model_name="chatsession",
            index=models.Index(
                fields=["user", "factory", "status", "-last_activity_at"],
                name="ai_chatsess_user_id_status_idx",
            ),
        ),
        migrations.RunPython(backfill_session_fields, migrations.RunPython.noop),
    ]
