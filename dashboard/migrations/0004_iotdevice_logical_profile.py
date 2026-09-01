from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0003_sitecode"),
    ]

    operations = [
        migrations.AddField(
            model_name="iotdevice",
            name="logical_profile",
            field=models.CharField(blank=True, max_length=128),
        ),
    ]
