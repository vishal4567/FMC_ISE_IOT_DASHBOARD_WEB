from django.db import migrations, models


def seed_site_codes(apps, schema_editor):
    """Seed the mapping table from the built-in site-code list so an existing
    deployment comes up pre-populated; edits then happen in the Config page."""
    SiteCode = apps.get_model("dashboard", "SiteCode")
    try:
        from integrations.location_map import HOSTNAME_SITE_MAP
    except Exception:
        HOSTNAME_SITE_MAP = []
    for site, codes in HOSTNAME_SITE_MAP:
        for code in codes:
            SiteCode.objects.get_or_create(
                code=code.strip(), defaults={"site": site, "active": True})


def unseed(apps, schema_editor):
    apps.get_model("dashboard", "SiteCode").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0002_snapshot"),
    ]

    operations = [
        migrations.CreateModel(
            name="SiteCode",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True,
                                        serialize=False, verbose_name="ID")),
                ("code", models.CharField(
                    help_text="Substring found in the NAD hostname, e.g. INBLRKOD",
                    max_length=64, unique=True)),
                ("site", models.CharField(
                    help_text="Friendly site name shown in the dashboard, e.g. Kodathi",
                    max_length=64)),
                ("active", models.BooleanField(default=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["site", "code"]},
        ),
        migrations.RunPython(seed_site_codes, unseed),
    ]
