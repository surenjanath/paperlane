from django.conf import settings
from django.db import models

from accounts.models import Workspace


class Notification(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    workspace = models.ForeignKey(Workspace, null=True, blank=True, on_delete=models.CASCADE)
    kind = models.CharField(max_length=30, default="info")
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True)
    link = models.CharField(max_length=300, blank=True)
    read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class DashboardWidget(models.Model):
    METRICS = [
        ("new_documents", "New documents"),
        ("completed_documents", "Completed documents"),
        ("declined_documents", "Declined documents"),
        ("sends", "Sends"),
        ("bot_executions", "Bot executions"),
        ("steps_completed", "Steps completed"),
        ("data_ingression", "Data ingression (field values submitted)"),
    ]
    AGGREGATIONS = [("day", "Day"), ("week", "Week"), ("month", "Month")]
    CHARTS = [("area", "Area"), ("line", "Line"), ("bar", "Bar")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="widgets")
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    title = models.CharField(max_length=120)
    metrics = models.JSONField(default=list)
    workflow_ids = models.JSONField(default=list, blank=True)
    range_days = models.PositiveIntegerField(default=7)
    aggregation = models.CharField(max_length=10, choices=AGGREGATIONS, default="day")
    chart = models.CharField(max_length=10, choices=CHARTS, default="area")
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]


class RecentItem(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="recents")
    workflow = models.ForeignKey("flows.Workflow", on_delete=models.CASCADE)
    opened_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "workflow")
        ordering = ["-opened_at"]


class HelpFeedback(models.Model):
    """“Was this helpful?” votes on help-center articles."""

    article = models.CharField(max_length=120, db_index=True)  # "<category>/<slug>"
    helpful = models.BooleanField()
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
