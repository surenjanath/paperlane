from django.urls import path

from . import views

app_name = "superadmin"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("workspaces/", views.workspaces, name="workspaces"),
    path("workspaces/<int:pk>/", views.workspace_detail, name="workspace"),
    path("users/", views.users, name="users"),
    path("users/<int:pk>/", views.user_detail, name="user"),
    path("settings/", views.settings_view, name="settings"),
    path("jobs/", views.jobs, name="jobs"),
    path("audit/", views.audit, name="audit"),
    path("system/", views.system, name="system"),
    path("sign-ins/", views.signins, name="signins"),
    path("broadcasts/", views.broadcasts, name="broadcasts"),
    path("tickets/", views.tickets, name="tickets"),
    path("sso/", views.sso, name="sso"),
    path("sso/<int:pk>/", views.sso, name="sso_edit"),
    path("export/<str:what>.csv", views.export_csv, name="export"),
]
