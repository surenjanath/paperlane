from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("flows", "0015_field_blocks"),
    ]

    operations = [
        migrations.AddField(
            model_name="aiusage",
            name="detail",
            field=models.TextField(blank=True),
        ),
    ]
