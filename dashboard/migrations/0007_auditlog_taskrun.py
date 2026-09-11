from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0006_appsetting"),
    ]

    operations = [
        migrations.CreateModel(
            name="AuditLog",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True,
                                        serialize=False, verbose_name="ID")),
                ("ts", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("username", models.CharField(blank=True, max_length=150)),
                ("action", models.CharField(db_index=True, max_length=64)),
                ("target", models.CharField(blank=True, max_length=200)),
                ("detail", models.CharField(blank=True, max_length=500)),
            ],
            options={"ordering": ["-ts"]},
        ),
        migrations.CreateModel(
            name="TaskRun",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True,
                                        serialize=False, verbose_name="ID")),
                ("task_id", models.CharField(blank=True, db_index=True, max_length=64)),
                ("name", models.CharField(db_index=True, max_length=120)),
                ("status", models.CharField(default="started", max_length=16)),
                ("started", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("finished", models.DateTimeField(blank=True, null=True)),
                ("runtime_ms", models.IntegerField(blank=True, null=True)),
                ("detail", models.CharField(blank=True, max_length=500)),
            ],
            options={"ordering": ["-started"]},
        ),
    ]
