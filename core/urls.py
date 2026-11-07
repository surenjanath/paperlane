from django.urls import path

from . import help_views, views

app_name = "core"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("welcome/", views.landing, name="landing"),
    path("analytics/", views.analytics, name="analytics"),
    path("analytics/widgets/new/", views.widget_edit, name="widget_new"),
    path("analytics/widgets/reorder/", views.widgets_reorder, name="widgets_reorder"),
    path("analytics/widgets/<int:pk>/", views.widget_edit, name="widget_edit"),
    path("analytics/widgets/<int:pk>/data/", views.widget_data, name="widget_data"),
    path("inbox/", views.inbox, name="inbox"),
    path("live.json", views.live, name="live"),
    path("search/", views.search, name="search"),
    path("notifications/", views.notifications, name="notifications"),
    path("notifications/read/", views.notifications_read, name="notifications_read"),
    path("notifications/<int:pk>/", views.notification_open, name="notification_open"),
    path("archive/", views.archive, name="archive"),
    path("audit/", views.audit, name="audit"),
    path("trash/", views.trash, name="trash"),
    path("trash/empty/", views.empty_trash, name="empty_trash"),
    path("pages/help/", help_views.legacy),
    path("pages/<str:page>/", views.static_page, name="page"),
    path("help/", help_views.home, name="help"),
    path("help/search.json", help_views.search_json, name="help_search"),
    path("help/ask/", help_views.ask, name="help_ask"),
    path("help/feedback/", help_views.feedback, name="help_feedback"),
    path("help/<slug:cat>/", help_views.category, name="help_category"),
    path("help/<slug:cat>/<slug:slug>/", help_views.article, name="help_article"),
]
