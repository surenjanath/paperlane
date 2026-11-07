from django.contrib import admin

from .models import DashboardWidget, Notification, RecentItem

admin.site.register((Notification, DashboardWidget, RecentItem))
