import secrets
import uuid

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property

from accounts.models import Contact, Group, Workspace


def option_keys(field, value):
    """Checkbox groups expose each option as its own boolean key: "<name>-<option value>"."""
    if field.get("type") != "checkbox" or not field.get("name") or not field.get("options"):
        return {}
    selected = value if isinstance(value, list) else []
    keys = field.get("option_values") or field["options"]
    return {f"{field['name']}-{k}": label in selected for label, k in zip(field["options"], keys)}


def new_key():
    return uuid.uuid4().hex[:12]


class Tag(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="tags")
    name = models.CharField(max_length=80)

    class Meta:
        unique_together = ("workspace", "name")
        ordering = ["name"]

    def __str__(self):
        return self.name


class WorkflowQuerySet(models.QuerySet):
    def live(self):
        return self.filter(archived_at__isnull=True, trashed_at__isnull=True)

    def visible_to(self, membership):
        qs = self.filter(workspace=membership.workspace)
        if membership.can_view_all:
            return qs
        return qs.filter(
            models.Q(created_by=membership.user)
            | models.Q(access=Workflow.TEAM)
            | models.Q(shared_users=membership.user)
            | models.Q(shared_groups__members=membership)
        ).distinct()


class Workflow(models.Model):
    WORKFLOW, TEMPLATE, DOCUMENT = "workflow", "template", "document"
    KINDS = [(WORKFLOW, "Workflow"), (TEMPLATE, "Template"), (DOCUMENT, "Document")]
    PRIVATE, TEAM = "private", "team"
    ACCESS = [(PRIVATE, "Private"), (TEAM, "Team")]
    DRAFT, PUBLISHED = "draft", "published"

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="workflows")
    kind = models.CharField(max_length=12, choices=KINDS, default=WORKFLOW)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    access = models.CharField(max_length=10, choices=ACCESS, default=PRIVATE)
    shared_users = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name="shared_workflows")
    shared_groups = models.ManyToManyField(Group, blank=True, related_name="shared_workflows")
    status = models.CharField(max_length=12, default=DRAFT, choices=[(DRAFT, "Unpublished"), (PUBLISHED, "Published")])
    has_unpublished_changes = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1)
    published_at = models.DateTimeField(null=True, blank=True)
    published_snapshot = models.JSONField(null=True, blank=True)
    tags = models.ManyToManyField(Tag, blank=True, related_name="workflows")
    is_hidden = models.BooleanField(default=False)
    archived_at = models.DateTimeField(null=True, blank=True)
    trashed_at = models.DateTimeField(null=True, blank=True)
    link_enabled = models.BooleanField(default=False)
    link_token = models.CharField(max_length=64, unique=True, default=secrets.token_urlsafe)
    hook_token = models.CharField(max_length=64, unique=True, default=secrets.token_urlsafe)
    welcome_enabled = models.BooleanField(default=False)
    welcome_message = models.TextField(blank=True)
    esign_consent = models.BooleanField(default=False)
    autostart = models.JSONField(default=dict, blank=True)
    options = models.JSONField(default=dict, blank=True)  # live settings, e.g. {"duplicates": {"fields": [...], "days": 0}}
    ROUTING_LINEAR, ROUTING_BOTS = "linear", "bots"
    routing_mode = models.CharField(
        max_length=10, default=ROUTING_LINEAR,
        choices=[(ROUTING_LINEAR, "Linear (step order + branches)"), (ROUTING_BOTS, "Diagram bots (routing bots decide the next step)")])
    source_ref = models.CharField(max_length=120, blank=True, help_text="Origin of an imported workflow")
    last_autostart_at = models.DateTimeField(null=True, blank=True)
    locked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    locked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = WorkflowQuerySet.as_manager()

    LOCK_MINUTES = 30

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("flows:workflow_detail", args=[self.pk])

    @property
    def is_published(self):
        return self.status == self.PUBLISHED

    def lock_holder(self, user):
        """Return the *other* user currently editing this workflow, if any."""
        if self.locked_by_id and self.locked_by_id != user.id and self.locked_at:
            if timezone.now() - self.locked_at < timezone.timedelta(minutes=self.LOCK_MINUTES):
                return self.locked_by
        return None

    def ordered_steps(self):
        return list(self.steps.order_by("order", "id").select_related("recipient_group", "recipient_contact"))

    def touch(self):
        self.has_unpublished_changes = True
        self.save(update_fields=["has_unpublished_changes", "updated_at"])

    # ---- snapshot -------------------------------------------------------------
    def build_snapshot(self):
        return {
            "workflow_id": self.pk,
            "name": self.name,
            "version": self.version,
            "esign_consent": self.esign_consent,
            "routing_mode": self.routing_mode,
            "documents": [d.as_dict() for d in self.documents.order_by("order", "id")],
            "steps": [s.as_dict() for s in self.steps.order_by("order", "id")],
            "bots": [b.as_dict() for b in self.bots.filter(enabled=True).order_by("order", "id")],
        }

    def export_snapshot(self):
        snap = self.build_snapshot()
        snap["bots"] = [dict(b.as_dict(), enabled=b.enabled) for b in self.bots.order_by("order", "id")]
        return snap

    def runnable_snapshot(self):
        return self.published_snapshot or self.build_snapshot()


class WorkflowVersion(models.Model):
    workflow = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name="versions")
    number = models.PositiveIntegerField()
    snapshot = models.JSONField()
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-number"]


class BuilderSnapshot(models.Model):
    """One entry on a workflow's undo or redo stack: the definition as it was before (undo) or after (redo) a change."""
    UNDO, REDO = "undo", "redo"
    workflow = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name="builder_snapshots")
    stack = models.CharField(max_length=4, choices=[(UNDO, "Undo"), (REDO, "Redo")], default=UNDO)
    label = models.CharField(max_length=120, blank=True)
    data = models.JSONField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        indexes = [models.Index(fields=["workflow", "stack", "-id"])]


class StepTemplate(models.Model):
    """A step saved for reuse in any workflow of the workspace: its settings, the fields it fills and its bots."""
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="step_templates")
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=255, blank=True)
    data = models.JSONField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    uses = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-uses", "name"]


class WorkflowDocument(models.Model):
    FORM, FILE = "form", "file"
    workflow = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name="documents")
    key = models.CharField(max_length=20, default=new_key)
    name = models.CharField(max_length=200, default="Untitled form")
    kind = models.CharField(max_length=10, default=FORM, choices=[(FORM, "Form"), (FILE, "Uploaded file")])
    schema = models.JSONField(default=dict, blank=True)
    design = models.JSONField(default=dict, blank=True)
    file = models.FileField(upload_to="workflow_files/", blank=True)
    condition = models.JSONField(default=dict, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.name

    @property
    def fields(self):
        return (self.schema or {}).get("fields", [])

    def input_fields(self):
        from .fieldtypes import is_input

        return [f for f in self.fields if is_input(f.get("type"))]

    def as_dict(self):
        return {
            "key": self.key,
            "name": self.name,
            "kind": self.kind,
            "fields": self.fields,
            "roles": (self.schema or {}).get("roles", []),
            "design": self.design or {},
            "file_url": self.file.url if self.file else "",
            # storage name, so a packet keeps using the file its version shipped with after the file is replaced
            "file_name": self.file.name if self.file else "",
            "condition": self.condition or {},
        }


class Step(models.Model):
    RECIPIENT, BRANCH, BOT = "recipient", "branch", "bot"
    KINDS = [(RECIPIENT, "Step"), (BRANCH, "Branches"), (BOT, "Bot")]
    R_EMAIL, R_CURRENT, R_CONTACT, R_GROUP, R_ANYONE = "email", "current_user", "contact", "group", "anyone"
    RECIPIENT_TYPES = [
        (R_EMAIL, "Email or phone"),
        (R_CURRENT, "Current user"),
        (R_CONTACT, "Integration contact"),
        (R_GROUP, "Group (any member)"),
        (R_ANYONE, "Anyone with the link"),
    ]

    workflow = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name="steps")
    key = models.CharField(max_length=20, default=new_key)
    order = models.PositiveIntegerField(default=0)
    kind = models.CharField(max_length=12, choices=KINDS, default=RECIPIENT)
    name = models.CharField(max_length=120, default="Recipient")
    recipient_type = models.CharField(max_length=20, choices=RECIPIENT_TYPES, default=R_EMAIL)
    recipient_email = models.CharField(max_length=254, blank=True)
    recipient_name = models.CharField(max_length=160, blank=True)
    recipient_contact = models.ForeignKey(Contact, null=True, blank=True, on_delete=models.SET_NULL)
    recipient_group = models.ForeignKey(Group, null=True, blank=True, on_delete=models.SET_NULL)
    message_subject = models.CharField(max_length=200, default="You're invited to complete the documents")
    message_body = models.TextField(default="Hi {user_name},\nYou're invited to fill out {workflow_name}.")
    attach_documents = models.BooleanField(default=False)
    document_access = models.JSONField(default=dict, blank=True)  # {doc_key: fill|view|hidden}
    revoke_on_complete = models.BooleanField(default=False)
    thank_you = models.JSONField(default=dict, blank=True)
    condition = models.JSONField(default=dict, blank=True)  # {"logic": "and", "rules": [...]}
    branches = models.JSONField(default=list, blank=True)  # [{"label", "logic", "rules", "target"}]
    settings = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.name

    def setting(self, key, default=None):
        return (self.settings or {}).get(key, default)

    @property
    def sla_text(self):
        from .sla import describe

        return describe({"settings": self.settings})

    def recipient_label(self):
        if self.kind != self.RECIPIENT:
            return ""
        if self.recipient_type == self.R_CURRENT:
            return "Current user"
        if self.recipient_type == self.R_ANYONE:
            return "Anyone with the link"
        if self.recipient_type == self.R_CONTACT and self.recipient_contact:
            return str(self.recipient_contact)
        if self.recipient_type == self.R_GROUP and self.recipient_group:
            return f"Group: {self.recipient_group.name}"
        return self.recipient_email or ""

    def fill_count(self):
        docs = self.workflow.documents.all()
        return sum(1 for d in docs if self.document_access.get(d.key, "fill") == "fill")

    def as_dict(self):
        return {
            "key": self.key,
            "order": self.order,
            "kind": self.kind,
            "name": self.name,
            "recipient_type": self.recipient_type,
            "recipient_email": self.recipient_email,
            "recipient_name": self.recipient_name,
            "recipient_contact": (
                {"id": self.recipient_contact_id, "name": self.recipient_contact.name, "email": self.recipient_contact.email}
                if self.recipient_contact else None
            ),
            "recipient_group": (
                {"id": self.recipient_group_id, "name": self.recipient_group.name} if self.recipient_group else None
            ),
            "message_subject": self.message_subject,
            "message_body": self.message_body,
            "attach_documents": self.attach_documents,
            "document_access": self.document_access or {},
            "revoke_on_complete": self.revoke_on_complete,
            "thank_you": self.thank_you or {},
            "condition": self.condition or {},
            "branches": self.branches or [],
            "settings": self.settings or {},
        }


class Bot(models.Model):
    STEP_START, STEP_COMPLETE, FLOW_COMPLETE, INLINE, FLOW_START, STEP_OPENED = (
        "step_start", "step_complete", "flow_complete", "inline", "flow_start", "step_opened")
    TRIGGERS = [
        (FLOW_START, "When the workflow starts"),
        (STEP_START, "When documents are sent to a step (pre-fill)"),
        (STEP_OPENED, "When recipients open the documents"),
        (STEP_COMPLETE, "When recipients complete documents"),
        (FLOW_COMPLETE, "When the whole workflow is completed"),
        (INLINE, "As a bot step in the workflow"),
    ]
    workflow = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name="bots")
    key = models.CharField(max_length=20, default=new_key)
    bot_type = models.CharField(max_length=60)
    name = models.CharField(max_length=160)
    trigger = models.CharField(max_length=20, choices=TRIGGERS, default=STEP_COMPLETE)
    step_key = models.CharField(max_length=20, blank=True, help_text="Blank = any step")
    config = models.JSONField(default=dict, blank=True)
    condition = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=True)
    run_once = models.BooleanField(default=False, help_text="Run at most once per document")
    success_tags = models.JSONField(default=list, blank=True, help_text="Tags added when the bot succeeds")
    delay_minutes = models.PositiveIntegerField(default=0, help_text="Run this long after the trigger")
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def as_dict(self):
        return {
            "key": self.key, "bot_type": self.bot_type, "name": self.name, "trigger": self.trigger,
            "step_key": self.step_key, "config": self.config or {}, "condition": self.condition or {},
            "run_once": self.run_once, "success_tags": self.success_tags or [], "delay_minutes": self.delay_minutes,
        }


class PacketQuerySet(models.QuerySet):
    def live(self):
        return self.filter(archived_at__isnull=True, trashed_at__isnull=True)

    def visible_to(self, membership):
        qs = self.filter(workspace=membership.workspace)
        if membership.can_view_all:
            return qs
        email = membership.user.email
        return qs.filter(
            models.Q(created_by=membership.user)
            | models.Q(workflow__created_by=membership.user)
            | models.Q(runs__assignee_user=membership.user)
            | models.Q(runs__assignee_email__iexact=email)
            | models.Q(runs__assignee_group__members=membership)  # steps assigned to one of their groups
        ).distinct()


class Packet(models.Model):
    """A running/finished instance of a workflow — called a "Document" in the UI."""

    IN_PROGRESS, COMPLETED, DECLINED, CANCELLED, DRAFT = "in_progress", "completed", "declined", "cancelled", "draft"
    STATUSES = [
        (DRAFT, "Draft"), (IN_PROGRESS, "In progress"), (COMPLETED, "Completed"),
        (DECLINED, "Declined"), (CANCELLED, "Cancelled"),
    ]
    SOURCES = [(x, x.title()) for x in ["manual", "link", "bulk", "autostart", "webhook", "workflow", "test", "api", "form"]]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="packets")
    workflow = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name="packets")
    name = models.CharField(max_length=255)
    version = models.PositiveIntegerField(default=1)
    definition = models.JSONField(default=dict)
    data = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=STATUSES, default=IN_PROGRESS)
    current_step_key = models.CharField(max_length=20, blank=True)
    visited = models.JSONField(default=list, blank=True)
    flags = models.JSONField(default=dict, blank=True)  # e.g. {"duplicates": [{"id", "name", "field", "value"}]}
    source = models.CharField(max_length=20, choices=SOURCES, default="manual")
    is_test = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    locked = models.BooleanField(default=False)
    tags = models.ManyToManyField(Tag, blank=True, related_name="packets")
    archived_at = models.DateTimeField(null=True, blank=True)
    trashed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    last_activity_at = models.DateTimeField(default=timezone.now)

    objects = PacketQuerySet.as_manager()

    class Meta:
        ordering = ["-last_activity_at"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("flows:packet_detail", args=[self.pk])

    # helpers over the frozen definition
    @property
    def steps(self):
        return self.definition.get("steps", [])

    @property
    def documents(self):
        return self.definition.get("documents", [])

    def step(self, key):
        return next((s for s in self.steps if s["key"] == key), None)

    def current_step(self):
        return self.step(self.current_step_key) if self.current_step_key else None

    def next_recipient_step(self):
        """Best-effort label for the "Next:" column."""
        from . import diagram

        steps = self.steps
        by_key = {s["key"]: s for s in steps}
        if self.current_step_key not in by_key:
            return None
        cur, hops = diagram.successor(steps, self.current_step_key), 0
        while cur in by_key and hops < 50:  # first recipient step on the default path (branches: after their lanes)
            if by_key[cur]["kind"] == "recipient":
                return by_key[cur]
            cur, hops = diagram.successor(steps, cur), hops + 1
        return None

    def progress(self):
        total = len([s for s in self.steps if s["kind"] == "recipient"]) or 1
        done = len({r.step_key for r in self.runs.all() if r.status in ("completed", "skipped")})  # uses prefetched runs
        if self.status == self.COMPLETED:
            done = total
        return min(done, total), total

    def progress_pct(self):
        done, total = self.progress()
        return int(done * 100 / total) if total else 0

    def flat_values(self):
        """{field name/label/id: value} across all documents — used by bots and conditions."""
        out = {}
        for doc in self.documents:
            values = self.data.get(doc["key"], {})
            for f in doc.get("fields", []):
                if f["id"] in values:
                    v = values[f["id"]]
                    out[f["id"]] = v
                    if f.get("name"):
                        out[f["name"]] = v
                    if f.get("label"):
                        out.setdefault(f["label"], v)
                    out.update(option_keys(f, v))
        return out

    @cached_property
    def next_due(self):
        """Earliest deadline among open steps (lists annotate this to save a query per row)."""
        if self.status != self.IN_PROGRESS:
            return None
        return self.runs.filter(status__in=["pending", "opened"], due_at__isnull=False).order_by("due_at") \
            .values_list("due_at", flat=True).first()

    @property
    def is_overdue(self):
        return bool(self.next_due and self.status == self.IN_PROGRESS and self.next_due <= timezone.now())

    @property
    def latest_run(self):
        runs = list(self.runs.all())
        return runs[-1] if runs else None

    def needs_attention(self):
        return self.status == self.DECLINED or self.runs.filter(status__in=["expired", "declined"]).exists()


class KeyDate(models.Model):
    """A date that matters after a document is finished (renewal, expiry, review) with reminders before it."""
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="key_dates")
    packet = models.ForeignKey(Packet, on_delete=models.CASCADE, related_name="key_dates")
    field_id = models.CharField(max_length=128)
    label = models.CharField(max_length=160)
    date = models.DateField(db_index=True)
    remind_days = models.JSONField(default=list, blank=True)  # e.g. [30, 7, 0]: days before the date
    sent = models.JSONField(default=list, blank=True)  # the remind_days already sent
    notify_emails = models.CharField(max_length=500, blank=True)
    done_at = models.DateTimeField(null=True, blank=True)  # someone marked it handled
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date"]
        constraints = [models.UniqueConstraint(fields=["packet", "field_id"], name="one_key_date_per_field")]


class DocIndex(models.Model):
    """Search text (and, with an embedding model, a vector) for one document — sensitive fields are never included."""
    packet = models.OneToOneField(Packet, on_delete=models.CASCADE, related_name="search_index")
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="doc_index")
    text = models.TextField()
    sig = models.CharField(max_length=40)
    vector = models.JSONField(null=True, blank=True)
    model = models.CharField(max_length=160, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class StepRun(models.Model):
    PENDING, OPENED, COMPLETED, DECLINED, SKIPPED, EXPIRED, REVOKED = (
        "pending", "opened", "completed", "declined", "skipped", "expired", "revoked")
    STATUSES = [(x, x.title()) for x in [PENDING, OPENED, COMPLETED, DECLINED, SKIPPED, EXPIRED, REVOKED]]

    packet = models.ForeignKey(Packet, on_delete=models.CASCADE, related_name="runs")
    step_key = models.CharField(max_length=20)
    step_name = models.CharField(max_length=120)
    assignee_email = models.CharField(max_length=254, blank=True)
    assignee_name = models.CharField(max_length=160, blank=True)
    assignee_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="step_runs")
    assignee_group = models.ForeignKey(Group, null=True, blank=True, on_delete=models.SET_NULL)
    token = models.CharField(max_length=48, unique=True, default=secrets.token_urlsafe)
    status = models.CharField(max_length=12, choices=STATUSES, default=PENDING)
    draft = models.JSONField(default=dict, blank=True)
    decline_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    opened_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    reminders_sent = models.PositiveIntegerField(default=0)
    last_reminder_at = models.DateTimeField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True, db_index=True)  # step deadline (SLA)
    escalated_at = models.DateTimeField(null=True, blank=True)
    delegated_from = models.CharField(max_length=254, blank=True)  # out-of-office: who it was meant for
    # group work queue: the member who took this step (others in the group can't work on it meanwhile)
    claimed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    claimed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "id"]

    @property
    def is_overdue(self):
        return bool(self.due_at and self.is_open and self.due_at <= timezone.now())

    @property
    def is_open(self):
        return self.status in (self.PENDING, self.OPENED)

    def fill_url(self):
        return reverse("flows:fill", args=[self.token])

    def absolute_fill_url(self):
        return settings.SITE_URL.rstrip("/") + self.fill_url()

    def claim(self, user, *, force=False):
        """Take this step for ``user``. Returns False when a teammate holds it (a single conditional UPDATE, so two
        people clicking at once can't both take it). ``force`` is for takeovers and assignment bots."""
        now = timezone.now()
        rows = StepRun.objects.filter(pk=self.pk)
        if not force:
            rows = rows.filter(models.Q(claimed_by__isnull=True) | models.Q(claimed_by=user))
        if rows.update(claimed_by=user, claimed_at=now):
            self.claimed_by, self.claimed_at = user, now
            return True
        self.refresh_from_db(fields=["claimed_by", "claimed_at"])
        return False

    def release(self):
        self.claimed_by, self.claimed_at = None, None
        self.save(update_fields=["claimed_by", "claimed_at"])

    def can_act(self, user):
        if not user.is_authenticated:
            return False
        if self.claimed_by_id and self.claimed_by_id != user.id:  # taken by a teammate
            return False
        if self.assignee_user_id == user.id:
            return True
        if self.assignee_email and self.assignee_email.lower() == (user.email or "").lower():
            return True
        if self.assignee_group_id:
            if self.assignee_group.members.filter(user=user).exists():
                return True
            from core.queries import covering_for  # delegate of a member who is out of office

            covering = covering_for(user)
            return bool(covering) and self.assignee_group.members.filter(user_id__in=covering).exists()
        return False


class PacketRevision(models.Model):
    packet = models.ForeignKey(Packet, on_delete=models.CASCADE, related_name="revisions")
    number = models.PositiveIntegerField()
    data = models.JSONField(default=dict)
    step_key = models.CharField(max_length=20, blank=True)
    step_name = models.CharField(max_length=120, blank=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    author_label = models.CharField(max_length=200, blank=True)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-number"]


class PacketFile(models.Model):
    packet = models.ForeignKey(Packet, on_delete=models.CASCADE, related_name="files")
    doc_key = models.CharField(max_length=20)
    field_id = models.CharField(max_length=128)
    file = models.FileField(upload_to="packet_files/%Y/%m/")
    original_name = models.CharField(max_length=255)
    uploaded_at = models.DateTimeField(auto_now_add=True)


class Comment(models.Model):
    packet = models.ForeignKey(Packet, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    author_label = models.CharField(max_length=200, blank=True)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class AuditEvent(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="audit_events")
    workflow = models.ForeignKey(Workflow, null=True, blank=True, on_delete=models.CASCADE, related_name="audit_events")
    packet = models.ForeignKey(Packet, null=True, blank=True, on_delete=models.CASCADE, related_name="audit_events")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    actor_label = models.CharField(max_length=200, blank=True)
    action = models.CharField(max_length=60)
    detail = models.TextField(blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class BotLog(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="bot_logs")
    workflow = models.ForeignKey(Workflow, null=True, blank=True, on_delete=models.CASCADE, related_name="bot_logs")
    packet = models.ForeignKey(Packet, null=True, blank=True, on_delete=models.CASCADE, related_name="bot_logs")
    bot_key = models.CharField(max_length=20)
    bot_name = models.CharField(max_length=160)
    trigger = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=10, choices=[("success", "Success"), ("failed", "Failed"), ("skipped", "Skipped")])
    message = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]


class BotJob(models.Model):
    """A bot run waiting for the worker: delayed bots, and bots set to run in the background. Failed runs are retried."""

    packet = models.ForeignKey(Packet, on_delete=models.CASCADE, related_name="bot_jobs")
    bot = models.JSONField()
    trigger = models.CharField(max_length=20)
    context = models.JSONField(default=dict, blank=True)
    run_at = models.DateTimeField(db_index=True)
    done = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=500, blank=True)
    locked_until = models.DateTimeField(null=True, blank=True)  # a worker is running it; others skip it until then

    class Meta:
        ordering = ["run_at"]


class AccessRequest(models.Model):
    workflow = models.ForeignKey(Workflow, on_delete=models.CASCADE, related_name="access_requests")
    requester = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=20, default="configure")
    status = models.CharField(max_length=10, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)


class DataSheet(models.Model):
    """Spreadsheet-like dataset filled by the "Export to spreadsheet" bot."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="datasheets")
    name = models.CharField(max_length=160)
    columns = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("workspace", "name")
        ordering = ["name"]


class DataSheetRow(models.Model):
    sheet = models.ForeignKey(DataSheet, on_delete=models.CASCADE, related_name="rows")
    packet = models.ForeignKey(Packet, null=True, blank=True, on_delete=models.SET_NULL)
    values = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class OutboundEmail(models.Model):
    """Copy of every email Paperlane sends, visible in the workspace's Email log."""

    workspace = models.ForeignKey(Workspace, null=True, on_delete=models.CASCADE, related_name="outbound_emails")
    packet = models.ForeignKey(Packet, null=True, blank=True, on_delete=models.SET_NULL)
    to = models.CharField(max_length=500)
    subject = models.CharField(max_length=255)
    body = models.TextField()
    kind = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class IntakeForm(models.Model):
    """A customizable public form that starts a workflow (replaces "web form → spreadsheet → workflow" chains).

    ``schema`` uses the document-editor field format. ``lookups`` are in-form bots: when a trigger field is filled
    the bot fetches data (Paperlane spreadsheet, earlier documents or an external API) and pre-fills form fields
    before the person submits. ``options`` (all optional):
      submit_label, success_title, success_body, redirect_url,
      recipient_email_field / recipient_name_field  — who receives the workflow's first step,
      complete_first_step  — the submission counts as the workflow's first step (it moves straight on),
      mapping {workflow field name: form field id}  — on top of the automatic match by field name/label,
      sheet  — also append each submission to this Paperlane spreadsheet,
      notify_emails, require_login, closed, closed_message, document_name (pattern with {field:X}).
    """

    DRAFT, PUBLISHED = "draft", "published"
    STATUSES = [(DRAFT, "Draft"), (PUBLISHED, "Published")]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="intake_forms")
    workflow = models.ForeignKey(Workflow, null=True, blank=True, on_delete=models.SET_NULL, related_name="intake_forms")
    name = models.CharField(max_length=200)
    description = models.CharField(max_length=400, blank=True)
    token = models.CharField(max_length=64, unique=True, default=secrets.token_urlsafe)
    status = models.CharField(max_length=12, choices=STATUSES, default=DRAFT)
    schema = models.JSONField(default=dict, blank=True)
    design = models.JSONField(default=dict, blank=True)
    options = models.JSONField(default=dict, blank=True)
    lookups = models.JSONField(default=list, blank=True)
    template_key = models.CharField(max_length=60, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.name

    @property
    def fields(self):
        return (self.schema or {}).get("fields", [])

    def input_fields(self):
        from .fieldtypes import is_input

        return [f for f in self.fields if is_input(f.get("type"))]

    @property
    def is_live(self):
        return self.status == self.PUBLISHED and not self.archived_at and not (self.options or {}).get("closed")

    def get_absolute_url(self):
        return reverse("flows:form_settings", args=[self.pk])

    def public_url(self):
        return reverse("flows:form_public", args=[self.token])

    def as_doc(self):
        """The form in the document dict shape used by the fill-page renderer and validator."""
        return {"key": "form", "name": self.name, "fields": self.fields, "design": self.design or {}, "kind": "form"}


class FormSubmission(models.Model):
    STARTED, NO_WORKFLOW, FAILED, AWAITING_PAYMENT = "started", "saved", "failed", "unpaid"
    STATUSES = [(STARTED, "Workflow started"), (NO_WORKFLOW, "Saved"), (FAILED, "Failed"), (AWAITING_PAYMENT, "Awaiting payment")]

    form = models.ForeignKey(IntakeForm, on_delete=models.CASCADE, related_name="submissions")
    data = models.JSONField(default=dict)
    packet = models.ForeignKey(Packet, null=True, blank=True, on_delete=models.SET_NULL, related_name="form_submissions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    status = models.CharField(max_length=12, choices=STATUSES, default=NO_WORKFLOW)
    error = models.TextField(blank=True)
    payment = models.JSONField(default=dict, blank=True)  # {"session", "amount", "currency", "status", "paid_at", "intent"}
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class FormFile(models.Model):
    """A file uploaded on a public form (kept across a failed submit, then attached to the started document)."""

    form = models.ForeignKey(IntakeForm, on_delete=models.CASCADE, related_name="files")
    submission = models.ForeignKey(FormSubmission, null=True, blank=True, on_delete=models.SET_NULL, related_name="files")
    field_id = models.CharField(max_length=60)
    key = models.CharField(max_length=64, default=secrets.token_urlsafe, unique=True)
    file = models.FileField(upload_to="forms/%Y/%m/")
    original_name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)


class WebhookEndpoint(models.Model):
    """Outgoing webhook: Paperlane POSTs signed JSON events to this URL."""
    EVENTS = [
        ("document.started", "Document started"), ("step.sent", "Step sent to a recipient"),
        ("step.completed", "Step completed"), ("step.declined", "Step declined"), ("step.reassigned", "Step reassigned"),
        ("step.overdue", "Step overdue (deadline passed)"), ("step.delegated", "Step delegated (out of office)"),
        ("document.completed", "Document completed"), ("document.cancelled", "Document cancelled"),
        ("form.submitted", "Start form submitted"), ("comment.added", "Comment added"),
    ]
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="webhooks")
    url = models.URLField(max_length=500)
    description = models.CharField(max_length=120, blank=True)
    events = models.JSONField(default=list)  # empty = every event
    workflow = models.ForeignKey(Workflow, null=True, blank=True, on_delete=models.CASCADE, related_name="webhooks")
    include_values = models.BooleanField(default=False)
    secret = models.CharField(max_length=64, default=secrets.token_hex)
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.url

    def wants(self, event, workflow_id=None):
        if not self.active:
            return False
        if self.workflow_id and workflow_id != self.workflow_id:
            return False
        return not self.events or event in self.events


class WebhookDelivery(models.Model):
    PENDING, OK, FAILED = "pending", "ok", "failed"
    endpoint = models.ForeignKey(WebhookEndpoint, on_delete=models.CASCADE, related_name="deliveries")
    event = models.CharField(max_length=40)
    uid = models.CharField(max_length=36, unique=True)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=10, default=PENDING, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    response_code = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True)
    error = models.CharField(max_length=300, blank=True)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class Connector(models.Model):
    """A custom connector: any HTTP/JSON API defined once and used by bots, start-form lookups and the API.

    actions: [{"key", "name", "method", "path", "query": {k: tmpl}, "body": "json template", "map": [{"path", "field"}]}]
    Templates use {field:Name} (document/form values) and {value} (the typed value in a form lookup)."""
    AUTH_NONE, AUTH_BEARER, AUTH_HEADER, AUTH_BASIC, AUTH_QUERY = "none", "bearer", "header", "basic", "query"
    AUTHS = [(AUTH_NONE, "No authentication"), (AUTH_BEARER, "Bearer token"), (AUTH_HEADER, "API key in a header"),
             (AUTH_BASIC, "Basic (user:password)"), (AUTH_QUERY, "API key in the URL query")]
    KIND_HTTP, KIND_SQLITE = "http", "sqlite"
    KINDS = [(KIND_HTTP, "HTTP / JSON API"), (KIND_SQLITE, "SQLite database (read-only)")]
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="connectors")
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=300, blank=True)
    kind = models.CharField(max_length=10, choices=KINDS, default=KIND_HTTP)
    # SQLite: a file inside one of settings.PAPERLANE_DATA_DIRS, opened read-only; actions hold {"sql": ...}
    database = models.CharField(max_length=255, blank=True)
    base_url = models.URLField(max_length=500, blank=True)
    auth_type = models.CharField(max_length=10, choices=AUTHS, default=AUTH_NONE)
    auth_name = models.CharField(max_length=80, blank=True, help_text="Header or query parameter name")
    secret = models.CharField(max_length=500, blank=True)
    headers = models.JSONField(default=dict, blank=True)
    actions = models.JSONField(default=list, blank=True)
    timeout = models.PositiveSmallIntegerField(default=15)
    active = models.BooleanField(default=True)
    last_status = models.CharField(max_length=200, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        unique_together = ("workspace", "name")

    def __str__(self):
        return self.name

    def action(self, key):
        return next((a for a in self.actions or [] if a.get("key") == key), None)


class AIUsage(models.Model):
    """One call to a language model (AI bots, Ask AI, fill assist, search…) — shown on My workspace › AI & models."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="ai_usage")
    packet = models.ForeignKey(Packet, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    kind = models.CharField(max_length=30)
    engine = models.CharField(max_length=120)
    ok = models.BooleanField(default=True)
    ms = models.PositiveIntegerField(default=0)
    chars_in = models.PositiveIntegerField(default=0)
    chars_out = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=300, blank=True)
    detail = models.TextField(blank=True)  # short model reply when the call failed after a response; never the prompt
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class SupportTicket(models.Model):
    """A problem report — filed automatically by the diagnosis assistant for high/critical problems, or by a person."""

    LOW, MEDIUM, HIGH, CRITICAL = "low", "medium", "high", "critical"
    SEVERITIES = [(LOW, "Low"), (MEDIUM, "Medium"), (HIGH, "High"), (CRITICAL, "Critical")]
    OPEN, IN_PROGRESS, WAITING, RESOLVED, CLOSED = "open", "in_progress", "waiting", "resolved", "closed"
    STATUSES = [(OPEN, "Open"), (IN_PROGRESS, "In progress"), (WAITING, "Waiting on customer"), (RESOLVED, "Resolved"),
                (CLOSED, "Closed")]
    ACTIVE = (OPEN, IN_PROGRESS, WAITING)

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="tickets")
    packet = models.ForeignKey(Packet, null=True, blank=True, on_delete=models.SET_NULL, related_name="tickets")
    workflow = models.ForeignKey(Workflow, null=True, blank=True, on_delete=models.SET_NULL, related_name="tickets")
    title = models.CharField(max_length=200)
    summary = models.TextField(blank=True)  # the AI (or rule-based) explanation
    findings = models.JSONField(default=list, blank=True)
    severity = models.CharField(max_length=10, choices=SEVERITIES, default=MEDIUM, db_index=True)
    impact = models.CharField(max_length=300, blank=True)
    affected_count = models.PositiveIntegerField(default=1)
    fingerprint = models.CharField(max_length=120, blank=True, db_index=True)
    source = models.CharField(max_length=10, default="manual", choices=[("auto", "Filed automatically"), ("assistant", "From the assistant"), ("manual", "Manual")])
    status = models.CharField(max_length=12, choices=STATUSES, default=OPEN, db_index=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    notes = models.JSONField(default=list, blank=True)  # [{"by": label, "text": ..., "at": iso, "staff": bool}]
    assignee = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="assigned_tickets")
    due_at = models.DateTimeField(null=True, blank=True, db_index=True)  # response target, from the severity
    first_response_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def ref(self):
        return f"T-{self.pk:04d}"

    @property
    def is_active(self):
        return self.status in self.ACTIVE

    def get_absolute_url(self):
        return reverse("flows:ticket", args=[self.pk])

    RESPONSE_HOURS = {LOW: 168, MEDIUM: 72, HIGH: 24, CRITICAL: 4}

    def set_target(self):
        """Response target from the severity (only ever tightens while nobody has responded)."""
        if self.first_response_at:
            return
        due = self.created_at + timezone.timedelta(hours=self.RESPONSE_HOURS.get(self.severity, 72))
        self.due_at = min(self.due_at, due) if self.due_at else due

    @property
    def response_overdue(self):
        return bool(self.is_active and not self.first_response_at and self.due_at and self.due_at < timezone.now())


class DocumentSeal(models.Model):
    """Tamper evidence for a completed document: a SHA-256 fingerprint of its content (values, signatures, file bytes,
    signing timeline) printed on the certificate, plus the frozen final PDF and its own hash."""

    packet = models.ForeignKey(Packet, on_delete=models.CASCADE, related_name="seals")
    number = models.PositiveIntegerField(default=1)  # 1 = first completion, +1 after each revision is completed again
    content_hash = models.CharField(max_length=64, db_index=True)
    code = models.CharField(max_length=24, unique=True)
    pdf = models.FileField(upload_to="sealed/%Y/%m/", blank=True)
    pdf_hash = models.CharField(max_length=64, blank=True, db_index=True)
    signers = models.JSONField(default=list, blank=True)
    superseded_at = models.DateTimeField(null=True, blank=True)
    sealed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-number"]

    def get_verify_url(self):
        return reverse("verify_code", args=[self.code])


class SavedView(models.Model):
    """A named set of Documents filters (the query string), private or shared with the workspace."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="saved_views")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    name = models.CharField(max_length=80)
    query = models.CharField(max_length=1000)
    shared = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]


class FieldChange(models.Model):
    """One field value changing on a document — who, when, from what to what (sensitive values are stored masked)."""

    packet = models.ForeignKey(Packet, on_delete=models.CASCADE, related_name="field_changes")
    doc_key = models.CharField(max_length=20)
    field_id = models.CharField(max_length=128)  # imported workflows can have long field ids
    label = models.CharField(max_length=200)
    old = models.TextField(blank=True)
    new = models.TextField(blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    actor_label = models.CharField(max_length=200, blank=True)
    source = models.CharField(max_length=120, blank=True)  # "Step: Manager approval", "Bot: Rename", "AI fill", …
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class FieldBlock(models.Model):
    """A reusable section of form fields (e.g. “Bank details”) shared across the workspace's forms."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="field_blocks")
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=300, blank=True)
    fields = models.JSONField(default=list)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    uses = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uses", "name"]
