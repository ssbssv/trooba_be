# Alter model_version length for longer factory fingerprints.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0002_chat_session_expiration"),
    ]

    operations = [
        migrations.AlterField(
            model_name="chatsession",
            name="model_version",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
