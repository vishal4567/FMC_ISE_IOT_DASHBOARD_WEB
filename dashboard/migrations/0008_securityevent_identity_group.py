from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0007_auditlog_taskrun"),
    ]

    operations = [
        migrations.AddField(
            model_name="securityevent",
            name="identity_group",
            field=models.CharField(blank=True, db_index=True, max_length=64),
        ),
    ]
