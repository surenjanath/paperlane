from django.contrib import admin

from . import models


@admin.register(models.Workflow)
class WorkflowAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "workspace", "status", "version", "updated_at")
    list_filter = ("kind", "status", "workspace")
    search_fields = ("name",)


@admin.register(models.Packet)
class PacketAdmin(admin.ModelAdmin):
    list_display = ("name", "workflow", "status", "version", "created_at")
    list_filter = ("status", "workspace")
    search_fields = ("name",)


for m in (models.Step, models.WorkflowDocument, models.Bot, models.StepRun, models.PacketRevision, models.AuditEvent,
          models.BotLog, models.Tag, models.Comment, models.DataSheet, models.OutboundEmail, models.WorkflowVersion):
    admin.site.register(m)
