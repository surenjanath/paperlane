"""First-run setup for a production install: the super admin and, optionally, the first workspace. Safe to re-run.

    PAPERLANE_ADMIN_PASSWORD=... python manage.py bootstrap --email admin@example.com --workspace "Acme Insurance"

An existing account keeps its password unless --reset-password is given; a workspace is only created when none exists.
"""
import datetime as dt
import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone


class Command(BaseCommand):
    help = "Create (or confirm) the super admin and the first workspace. Password comes from PAPERLANE_ADMIN_PASSWORD."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--first-name", default="Admin")
        parser.add_argument("--last-name", default="")
        parser.add_argument("--workspace", default="", help="name of the first workspace (skipped if one exists)")
        parser.add_argument("--reset-password", action="store_true", help="set the password on an existing account")

    @transaction.atomic
    def handle(self, *args, **o):
        from accounts.models import Membership, Profile, Workspace
        from superadmin.models import get_settings

        User = get_user_model()
        email = o["email"].strip().lower()
        password = os.environ.get("PAPERLANE_ADMIN_PASSWORD", "")
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            if len(password) < 12:
                raise CommandError("Set PAPERLANE_ADMIN_PASSWORD (12+ characters) to create the super admin.")
            base = email.split("@")[0][:140]
            username, n = base, 1
            while User.objects.filter(username__iexact=username).exists():
                n += 1
                username = f"{base}{n}"
            user = User.objects.create_user(username=username, email=email, password=password,
                                            first_name=o["first_name"], last_name=o["last_name"])
            self.stdout.write(f"Created super admin {email}")
        elif o["reset_password"]:
            if len(password) < 12:
                raise CommandError("Set PAPERLANE_ADMIN_PASSWORD (12+ characters) to reset the password.")
            user.set_password(password)
            self.stdout.write(f"Password reset for {email}")
        else:
            self.stdout.write(f"Super admin {email} already exists (password unchanged)")
        user.is_superuser = user.is_staff = user.is_active = True
        user.save()
        Profile.objects.get_or_create(user=user)

        name = o["workspace"].strip()
        if name and not Workspace.objects.exists():
            ps = get_settings()
            ws = Workspace.objects.create(name=name, plan=ps.default_plan, credits_limit=ps.default_credits, email=email,
                                          plan_expires=(timezone.now() + dt.timedelta(days=ps.default_plan_days)).date())
            Membership.objects.create(user=user, workspace=ws, role=Membership.OWNER, position="Administrator")
            self.stdout.write(f"Created workspace “{ws.name}” owned by {email}")
        elif name:
            self.stdout.write("A workspace already exists; none created")
