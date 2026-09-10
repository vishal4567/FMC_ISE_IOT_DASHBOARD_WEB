from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0004_iotdevice_logical_profile"),
    ]

    operations = [
        migrations.AddField(
            model_name="iotdevice",
            name="authorization_profile",
            field=models.CharField(blank=True, max_length=128),
        ),
    ]
