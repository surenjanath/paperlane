from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from accounts.views import accept_invite
from core import views as core_views
from flows.views import api, verify

urlpatterns = [
    path("admin/", admin.site.urls),
    path("platform/", include("superadmin.urls")),
    path("invite/<str:token>/", accept_invite, name="accept_invite"),
    path("healthz/", core_views.healthz, name="healthz"),
    path("verify/", verify.verify, name="verify"),
    path("verify/<str:code>/", verify.verify, name="verify_code"),
    # token-authenticated HTTP access (remote MCP + REST API)
    path("mcp", api.mcp_http, name="mcp_http"),
    path("mcp/", api.mcp_http),
    path("api/v1/", api.rest, name="api_root"),
    path("api/v1/<str:resource>", api.rest, name="api"),
    path("api/v1/<str:resource>/<int:pk>", api.rest, name="api_item"),
    path("api/v1/<str:resource>/<int:pk>/<str:action>", api.rest, name="api_item_action"),
    path("api/v1/<str:resource>/<str:action>", api.rest, name="api_named"),
    path("", include("accounts.urls")),
    path("", include("flows.urls")),
    path("", include("core.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
