# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import models, migrations


class Migration(migrations.Migration):

    dependencies = [
        ('zerver', '0022_subscription_pin_to_top'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='default_language',
            field=models.CharField(default=b'en', max_length=50),
        ),
    ]
