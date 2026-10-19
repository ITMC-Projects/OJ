from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('judge', '0215_ots_password_digest')]
    operations = [migrations.AddField(
        model_name='otsaccountlink', name='source_credential_version',
        field=models.PositiveBigIntegerField(default=0, editable=False),
    ), migrations.RunPython(migrations.RunPython.noop)]
