from django.contrib import admin

from . import models

for m in (models.Workspace, models.Membership, models.Group, models.Invitation, models.Profile, models.Contact,
          models.Connection, models.EmailTemplate, models.UsageEvent):
    admin.site.register(m)
