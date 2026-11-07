from django.contrib.auth import views as auth_views
from django.urls import path, reverse_lazy

from . import security_views, sso, views

app_name = "accounts"

urlpatterns = [
    path("accounts/login/", views.login_view, name="login"),
    path("accounts/sso/<int:pk>/", sso.start, name="sso_start"),
    path("accounts/sso/callback/", sso.callback, name="sso_callback"),
    path("accounts/login/demo/", views.demo_login, name="demo_login"),
    path("accounts/login/verify/", security_views.login_verify, name="login_verify"),
    path("accounts/signup/", views.signup_view, name="signup"),
    path("accounts/logout/", views.logout_view, name="logout"),
    path("accounts/password-reset/", auth_views.PasswordResetView.as_view(
        template_name="accounts/password_reset.html", email_template_name="accounts/emails/password_reset.txt",
        subject_template_name="accounts/emails/password_reset_subject.txt",
        success_url=reverse_lazy("accounts:password_reset_done")), name="password_reset"),
    path("accounts/password-reset/sent/", auth_views.PasswordResetDoneView.as_view(
        template_name="accounts/password_reset_done.html"), name="password_reset_done"),
    path("accounts/reset/<uidb64>/<token>/", auth_views.PasswordResetConfirmView.as_view(
        template_name="accounts/password_reset_confirm.html", success_url=reverse_lazy("accounts:password_reset_complete")),
        name="password_reset_confirm"),
    path("accounts/reset/done/", auth_views.PasswordResetCompleteView.as_view(
        template_name="accounts/password_reset_complete.html"), name="password_reset_complete"),
    path("accounts/onboarding/", views.onboarding, name="onboarding"),
    path("accounts/switch/<int:pk>/", views.switch_workspace, name="switch"),
    # My account
    path("account/profile/", views.profile, name="profile"),
    path("account/settings/", views.account_settings, name="settings"),
    path("account/notifications/", views.notification_settings, name="notifications"),
    path("account/activity/", views.my_activity, name="activity"),
    path("account/security/", security_views.security, name="security"),
    path("account/security/<str:action>/", security_views.security_action, name="security_action"),
    path("account/api-tokens/", security_views.api_tokens, name="api_tokens"),
    path("account/out-of-office/", security_views.out_of_office, name="out_of_office"),
    path("account/connections/", views.connections, name="connections"),
    path("account/connections/oauth/<str:provider>/start/", views.oauth_start, name="oauth_start"),
    path("account/connections/oauth/<str:provider>/callback/", views.oauth_callback, name="oauth_callback"),
    # My workspace
    path("workspace/info/", views.workspace_info, name="workspace_info"),
    path("workspace/leave/", views.leave_workspace, name="leave_workspace"),
    path("workspace/settings/", views.workspace_settings, name="workspace_settings"),
    path("workspace/retention/", views.retention_settings, name="retention"),
    path("workspace/theme/", views.theme, name="theme"),
    path("workspace/thank-you/", views.thank_you, name="thank_you"),
    path("workspace/email-templates/", views.email_templates, name="email_templates"),
    path("workspace/domains/", views.domains, name="domains"),
    path("workspace/subscription/", views.subscription, name="subscription"),
    path("workspace/billing/", views.billing, name="billing"),
    path("workspace/usage/", views.usage_log, name="usage"),
    path("workspace/email-log/", views.email_log, name="email_log"),
    path("workspace/data/", views.datasheets, name="datasheets"),
    path("workspace/data/<int:pk>/", views.datasheets, name="datasheet"),
    # Team
    path("team/", views.team, name="team"),
    path("team/invite/", views.team_invite, name="team_invite"),
    path("team/add-to-group/", views.team_bulk_group, name="team_bulk_group"),
    path("team/invitations/<int:pk>/cancel/", views.invite_cancel, name="invite_cancel"),
    path("team/groups/", views.groups, name="groups"),
    path("team/groups/new/", views.group_panel, name="group_new"),
    path("team/groups/<int:pk>/", views.group_panel, name="group_panel"),
    path("team/<int:pk>/", views.team_member_panel, name="team_member_panel"),
    path("team/<int:pk>/<str:action>/", views.team_member, name="team_member"),
    # Contacts
    path("contacts/", views.contacts, name="contacts"),
    path("contacts/new/", views.contact_edit, name="contact_new"),
    path("contacts/<int:pk>/", views.contact_panel, name="contact_panel"),
    path("contacts/<int:pk>/edit/", views.contact_edit, name="contact_edit"),
    path("contacts/delete/", views.contact_delete, name="contact_delete"),
    path("contacts/import/", views.contacts_import, name="contacts_import"),
    path("contacts/export/", views.contacts_export, name="contacts_export"),
]
