"""Paperlane MCP server core: the tools AI assistants use to build, publish, run and inspect workflows and forms.

Transports: stdio (``manage.py paperlane_mcp``), Streamable HTTP (``POST /mcp`` with a personal API token) and the
REST API (``/api/v1/…``), which calls the same tools. The server acts as one Paperlane user inside one workspace,
with that user's role permissions; read-only tokens only see READ_TOOLS.
"""
import json
import sys
import traceback


SUPPORTED_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
INSTRUCTIONS = (
    "Paperlane automates document workflows. To build one: call get_build_guide, write a blueprint (documents with "
    "fields, steps with recipients, branch lanes, jumps, bots, optional start_form) and call build_workflow with "
    "publish=true. Check 'problems' in the result. To test: start_workflow with prefill values, then complete_step "
    "for each open step (get_document shows what is open), or submit_form for a start form. Everything created stays "
    "editable in the Paperlane builder (builder_url)."
)


def _obj(**props):
    required = [k for k, v in props.items() if v.pop("_required", False)]
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def P(type_, desc, required=False, **extra):
    d = {"type": type_, "description": desc, **extra}
    if required:
        d["_required"] = True
    return d


TOOLS = [
    ("get_build_guide", "Reference for writing blueprints: format, field types, condition operators and special "
                        "fields, bot types with their config, triggers, recipient types and tips. Call this first.", _obj()),
    ("list_workflows", "List workflows in the workspace (id, name, status, steps, documents).",
     _obj(query=P("string", "Filter by name"))),
    ("get_workflow", "Get a workflow as a blueprint (documents, fields, steps incl. branch lanes and jumps, bots, autostarts).",
     _obj(workflow_id=P("integer", "Workflow id", True))),
    ("build_workflow", "Create a complete workflow from a blueprint (see get_build_guide). Returns id, builder_url and "
                       "any problems. Use publish=true to make it runnable.",
     _obj(blueprint=P("object", "The workflow blueprint", True), publish=P("boolean", "Publish after building (default true)"),
          replace=P("boolean", "Move existing workflows with the same name to the Trash first (default false)"))),
    ("publish_workflow", "Publish a workflow (validates it first).", _obj(workflow_id=P("integer", "Workflow id", True))),
    ("trash_workflow", "Move a workflow to the Trash (it can be restored from the Trash).",
     _obj(workflow_id=P("integer", "Workflow id", True))),
    ("start_workflow", "Start a published workflow (creates a document). prefill maps field names to values.",
     _obj(workflow_id=P("integer", "Workflow id", True), prefill=P("object", "Field name → value"),
          recipient_email=P("string", "Send the first step to this email"), document_name=P("string", "Optional name"),
          test=P("boolean", "Test run: every step goes to you (default false)"))),
    ("list_documents", "List documents (running or finished workflow instances).",
     _obj(workflow_id=P("integer", "Only this workflow"), status=P("string", "in_progress | completed | declined | cancelled"),
          limit=P("integer", "Max rows (default 20)"))),
    ("get_document", "A document's status, step history, open steps (with fill links and their editable fields) and values.",
     _obj(document_id=P("integer", "Document id", True))),
    ("complete_step", "Fill and complete the open step of a document, as you (for testing). values maps field names "
                      "(or labels) to values; returns validation errors if required fields are missing.",
     _obj(document_id=P("integer", "Document id", True), step_name=P("string", "Which open step (default: the first open one)"),
          values=P("object", "Field name → value"))),
    ("list_form_templates", "Ready-made start form templates.", _obj()),
    ("list_forms", "List start forms.", _obj()),
    ("get_form", "A start form's fields, in-form bots, settings, link and recent submissions.",
     _obj(form_id=P("integer", "Form id", True))),
    ("build_form", "Create a start form: name, fields (editor field format), lookups (in-form bots), options "
                   "(submit_label, complete_first_step, recipient_email_field, sheet, …), workflow (name) or workflow_id, "
                   "template (key), publish.", _obj(form=P("object", "The form spec", True))),
    ("publish_form", "Publish a start form (opens its public link).", _obj(form_id=P("integer", "Form id", True))),
    ("run_form_lookup", "Test an in-form bot: look a value up and see which fields it would fill.",
     _obj(form_id=P("integer", "Form id", True), lookup_id=P("string", "Lookup id (see get_form)", True),
          value=P("string", "The value typed into the trigger field", True))),
    ("submit_form", "Submit a start form with values (field ids or names) as a test; in-form bots run and the linked "
                    "workflow starts.", _obj(form_id=P("integer", "Form id", True), values=P("object", "Field id/name → value", True))),
    ("list_team", "Teammates (email, role) and groups (name, members).", _obj()),
    ("create_group", "Create a group (or add members to an existing one) to use as a step recipient.",
     _obj(name=P("string", "Group name", True), member_emails=P("array", "Teammate emails", items={"type": "string"}))),
    ("list_spreadsheets", "Paperlane spreadsheets (used by lookups and export bots).", _obj()),
    ("get_spreadsheet", "Columns and rows of a spreadsheet.",
     _obj(name=P("string", "Spreadsheet name", True), limit=P("integer", "Max rows (default 50)"))),
    ("create_spreadsheet", "Create a spreadsheet (e.g. reference data for an in-form bot lookup) and add rows.",
     _obj(name=P("string", "Name", True), columns=P("array", "Column names", True, items={"type": "string"}),
          rows=P("array", "Rows: objects keyed by column", items={"type": "object"}))),
    ("list_connectors", "Custom connectors (HTTP/JSON APIs) and their actions — use them in blueprints with the "
                        "'connector_action' bot (config: connector id, action key) or in form lookups (source 'connector').",
     _obj()),
    ("run_connector", "Call a connector action now (it may change data in the other system). Returns status, mapped fields "
                      "and the JSON response.",
     _obj(connector_id=P("integer", "Connector id", True), action=P("string", "Action key", True),
          values=P("object", "Field values for {field:Name} tokens"), value=P("string", "Value for {value}"))),
    ("ask_document", "Ask the workspace AI model (e.g. local Ollama) a question about one document's field values.",
     _obj(document_id=P("integer", "Document id", True), question=P("string", "Question", True))),
    ("check_workflow", "Run the builder's checks on a workflow: broken links to deleted steps, rules or {field:…} tokens on "
                       "missing fields, branches that can never run, steps without recipients. Errors block publishing.",
     _obj(workflow_id=P("integer", "Workflow id", True))),
    ("explain_workflow", "A plain-English explanation of a workflow (who does what, decisions, automatic steps, deadlines) "
                         "written by the workspace AI model; cached until the workflow changes.",
     _obj(workflow_id=P("integer", "Workflow id", True))),
    ("workflow_activity", "Live activity per step: documents waiting, overdue, and the typical time each step takes.",
     _obj(workflow_id=P("integer", "Workflow id", True))),
    ("list_key_dates", "Upcoming key dates (renewals, expiries, reviews) from finished documents.",
     _obj(days=P("integer", "How many days ahead (default 90)"))),
    ("search_documents", "Find documents by meaning and words, and get an answer that cites them as [#id] "
                         "(only documents this user may see; sensitive fields are never searched).",
     _obj(query=P("string", "What to look for, in plain language", True))),
]


READ_TOOLS = {"get_build_guide", "list_workflows", "get_workflow", "list_documents", "get_document", "list_form_templates",
              "list_forms", "get_form", "run_form_lookup", "list_team", "list_spreadsheets", "get_spreadsheet",
              "list_connectors", "ask_document", "check_workflow", "explain_workflow", "workflow_activity", "list_key_dates", "search_documents"}


class ToolError(Exception):
    pass


class Server:
    def __init__(self, workspace, user, read_only=False):
        from accounts.models import Membership

        self.ws, self.user, self.read_only = workspace, user, read_only
        self.m = Membership.objects.get(workspace=workspace, user=user, active=True)

    def tool_names(self):
        return [n for n, _, _ in TOOLS if not self.read_only or n in READ_TOOLS]

    # ---- helpers ------------------------------------------------------------------------------
    def need(self, cap, what):
        if not getattr(self.m, cap):
            raise ToolError(f"Your role ({self.m.get_role_display()}) cannot {what}.")

    def url(self, path):
        from flows import engine

        return engine.absolute(path)

    def wf(self, pk, edit=False):
        from flows.models import Workflow
        from flows.permissions import can_edit_workflow

        wf = Workflow.objects.visible_to(self.m).filter(pk=pk).first()
        if wf is None:
            raise ToolError(f"No workflow {pk} in this workspace.")
        if edit and not can_edit_workflow(self.m, wf):
            raise ToolError("You cannot edit this workflow.")
        return wf

    def packet(self, pk):
        from flows.models import Packet

        p = Packet.objects.visible_to(self.m).filter(pk=pk).first()
        if p is None:
            raise ToolError(f"No document {pk} in this workspace.")
        return p

    def form(self, pk):
        from flows.models import IntakeForm

        f = IntakeForm.objects.filter(workspace=self.ws, pk=pk).first()
        if f is None:
            raise ToolError(f"No form {pk}.")
        return f

    # ---- tools ----------------------------------------------------------------------------------
    def t_get_build_guide(self):
        from flows import blueprint

        return blueprint.guide()

    def t_list_workflows(self, query=""):
        from flows.models import Workflow

        qs = Workflow.objects.visible_to(self.m).live().filter(kind="workflow")
        if query:
            qs = qs.filter(name__icontains=query)
        return [{"id": w.pk, "name": w.name, "status": w.status, "steps": w.steps.count(), "documents": w.packets.count(),
                 "builder_url": self.url(f"/workflows/{w.pk}/builder/")} for w in qs.order_by("name")[:200]]

    def t_get_workflow(self, workflow_id):
        from flows import blueprint

        return blueprint.export_workflow(self.wf(workflow_id))

    def t_build_workflow(self, blueprint, publish=True, replace=False):
        from django.utils import timezone

        from flows import blueprint as bp
        from flows import engine
        from flows.models import Workflow
        from flows.permissions import can_edit_workflow

        self.need("can_create", "create workflows")
        replaced = []
        if replace and isinstance(blueprint, dict) and blueprint.get("name"):
            for old in Workflow.objects.visible_to(self.m).live().filter(name=str(blueprint["name"]).strip()):
                if can_edit_workflow(self.m, old):
                    old.trashed_at = timezone.now()
                    old.save(update_fields=["trashed_at"])
                    replaced.append(old.pk)
        try:
            wf, problems = bp.build_workflow(self.ws, self.user, blueprint, publish=publish)
        except bp.BlueprintError as exc:
            raise ToolError(str(exc))
        engine.audit(self.ws, "workflow.mcp", f"Built “{wf.name}” via MCP", actor=self.user, workflow=wf)
        forms = list(wf.intake_forms.all())
        return {"id": wf.pk, "name": wf.name, "published": wf.is_published, "problems": problems, "replaced": replaced or None,
                "steps": wf.steps.count(), "documents": wf.documents.count(), "bots": wf.bots.count(),
                "builder_url": self.url(f"/workflows/{wf.pk}/builder/"),
                "start_forms": [{"id": f.pk, "name": f.name, "status": f.status, "public_url": self.url(f.public_url())} for f in forms]}

    def t_publish_workflow(self, workflow_id):
        from flows import services
        from flows.views.builder import validate

        wf = self.wf(workflow_id, edit=True)
        problems = validate(wf)
        if problems:
            return {"published": False, "problems": problems}
        services.publish(wf, self.user)
        wf.refresh_from_db()
        return {"published": True, "version": wf.version}

    def t_trash_workflow(self, workflow_id):
        from django.utils import timezone

        wf = self.wf(workflow_id, edit=True)
        wf.trashed_at = timezone.now()
        wf.save(update_fields=["trashed_at"])
        return {"trashed": True, "id": wf.pk, "note": "Restore it from the Trash page in Paperlane."}

    def t_start_workflow(self, workflow_id, prefill=None, recipient_email="", document_name="", test=False):
        from django.contrib.auth import get_user_model

        from flows import engine

        self.need("can_run", "start workflows")
        wf = self.wf(workflow_id)
        if not wf.is_published:
            raise ToolError("Publish the workflow first (publish_workflow).")
        assignee = None
        if recipient_email:
            assignee = {"email": recipient_email, "name": recipient_email,
                        "user": get_user_model().objects.filter(email__iexact=recipient_email).first()}
        p = engine.start_packet(wf, user=self.user, source="api", prefill=prefill or {}, first_assignee=assignee,
                                name=document_name or None, is_test=bool(test))
        return self._doc_summary(p)

    def _doc_summary(self, p):
        p.refresh_from_db()
        open_runs = p.runs.filter(status__in=["pending", "opened"]).order_by("id")
        return {"document_id": p.pk, "name": p.name, "status": p.status, "workflow": p.workflow.name,
                "history": [{"step": r.step_name, "status": r.status, "to": r.assignee_email or (r.assignee_group.name if r.assignee_group else r.assignee_name)}
                            for r in p.runs.order_by("id")],
                "open_steps": [{"step": r.step_name, "fill_url": r.absolute_fill_url(),
                                "to": r.assignee_email or (r.assignee_group.name if r.assignee_group else r.assignee_name)} for r in open_runs],
                "tags": sorted(p.tags.values_list("name", flat=True)), "url": self.url(p.get_absolute_url())}

    def t_list_documents(self, workflow_id=None, status="", limit=20):
        from flows.models import Packet

        qs = Packet.objects.visible_to(self.m).live().select_related("workflow").order_by("-created_at")
        if workflow_id:
            qs = qs.filter(workflow_id=workflow_id)
        if status:
            qs = qs.filter(status=status)
        return [{"document_id": p.pk, "name": p.name, "workflow": p.workflow.name, "status": p.status,
                 "current_step": (p.current_step() or {}).get("name"), "created": p.created_at.isoformat(timespec="minutes")}
                for p in qs.distinct()[: max(1, min(int(limit or 20), 200))]]

    def t_get_document(self, document_id):
        from flows import engine

        p = self.packet(document_id)
        out = self._doc_summary(p)
        values = {}
        for k, v in p.flat_values().items():
            if isinstance(v, str) and v.startswith("data:image"):
                v = "(signature)"
            if not isinstance(v, (dict, list)) or k in values:
                values.setdefault(k, v)
        out["values"] = {k: v for k, v in values.items() if not str(k).startswith(("f_",))}
        for o, r in zip(out["open_steps"], p.runs.filter(status__in=["pending", "opened"]).order_by("id")):
            ed = engine.editable_field_ids(p, r.step_key)
            o["editable_fields"] = [{"name": f.get("name") or f.get("label"), "type": f.get("type"), "required": bool(f.get("required")),
                                     "options": f.get("options")}
                                    for d in engine.visible_documents(p, r.step_key) for f in d.get("fields", [])
                                    if f["id"] in ed.get(d["key"], set()) and f.get("type") not in ("formula",)]
        return out

    def t_complete_step(self, document_id, step_name="", values=None):
        from flows import engine
        from flows.conditions import evaluate  # noqa: F401  (validation uses conditions)
        from flows.views.fill import validate

        p = self.packet(document_id)
        runs = p.runs.filter(status__in=["pending", "opened"]).order_by("id")
        if step_name:
            runs = runs.filter(step_name__iexact=step_name)
        run = runs.first()
        if run is None:
            raise ToolError("No open step" + (f" named '{step_name}'" if step_name else "") + " on this document.")
        if not (self.m.can_manage or run.can_act(self.user) or p.is_test):
            raise ToolError("Only the step's recipient or a supervisor/owner can complete it.")
        editable = engine.editable_field_ids(p, run.step_key)
        lowered = {str(k).strip().lower(): v for k, v in (values or {}).items()}
        values_by_doc, unknown = {}, set(lowered)
        for d in engine.visible_documents(p, run.step_key):
            vals = {}
            for f in d.get("fields", []):
                if f["id"] not in editable.get(d["key"], set()):
                    continue
                for cand in (f["id"], f.get("name"), f.get("label")):
                    if cand and str(cand).strip().lower() in lowered:
                        vals[f["id"]] = lowered[str(cand).strip().lower()]
                        unknown.discard(str(cand).strip().lower())
                        break
            values_by_doc[d["key"]] = vals
        merged = {k: dict(v) for k, v in (p.data or {}).items()}
        for dk, vals in values_by_doc.items():
            merged.setdefault(dk, {}).update(vals)
        from flows.models import Packet

        flat = Packet(definition=p.definition, data=merged).flat_values()
        errors = {}
        for d in engine.visible_documents(p, run.step_key):
            for fid, msg in validate(d, {**(p.data or {}).get(d["key"], {}), **values_by_doc.get(d["key"], {})},
                                     editable.get(d["key"], set()), flat).items():
                label = next((f.get("name") or f.get("label") for f in d["fields"] if f["id"] == fid), fid)
                errors[label] = msg
        if errors:
            return {"completed": False, "step": run.step_name, "errors": errors,
                    "ignored_values": sorted(unknown) or None}
        engine.complete_run(run, values_by_doc, user=self.user)
        out = self._doc_summary(p)
        out["completed_step"] = run.step_name
        if unknown:
            out["ignored_values"] = sorted(unknown)
        return out

    def t_list_form_templates(self):
        from flows.form_templates import TEMPLATES

        return [{"key": k, "name": t["name"], "category": t["category"], "description": t["description"],
                 "fields": [f.get("name") or f["label"] for f in t["fields"] if f["type"] not in ("heading",)],
                 "in_form_bots": [lk.get("name") for lk in t["lookups"]]} for k, t in TEMPLATES.items()]

    def t_list_forms(self):
        from flows.models import IntakeForm

        return [{"id": f.pk, "name": f.name, "status": f.status, "workflow": f.workflow.name if f.workflow else None,
                 "submissions": f.submissions.count(), "public_url": self.url(f.public_url())}
                for f in IntakeForm.objects.filter(workspace=self.ws, archived_at__isnull=True)]

    def t_get_form(self, form_id):
        f = self.form(form_id)
        return {"id": f.pk, "name": f.name, "status": f.status, "live": f.is_live,
                "workflow": {"id": f.workflow.pk, "name": f.workflow.name} if f.workflow else None,
                "fields": f.fields, "lookups": f.lookups, "options": f.options, "public_url": self.url(f.public_url()),
                "settings_url": self.url(f"/forms/{f.pk}/"),
                "recent_submissions": [{"id": s.pk, "status": s.status, "document_id": s.packet_id, "at": s.created_at.isoformat(timespec="minutes")}
                                       for s in f.submissions.all()[:10]]}

    def t_build_form(self, form):
        from flows import blueprint as bp

        self.need("can_create", "create forms")
        try:
            f = bp.build_form(self.ws, self.user, form)
        except bp.BlueprintError as exc:
            raise ToolError(str(exc))
        return {"id": f.pk, "name": f.name, "status": f.status, "workflow": f.workflow.name if f.workflow else None,
                "public_url": self.url(f.public_url()), "settings_url": self.url(f"/forms/{f.pk}/")}

    def t_publish_form(self, form_id):
        from flows.models import IntakeForm

        self.need("can_create", "publish forms")
        f = self.form(form_id)
        if not f.input_fields():
            raise ToolError("The form has no fields.")
        f.status = IntakeForm.PUBLISHED
        f.save(update_fields=["status"])
        return {"published": True, "public_url": self.url(f.public_url())}

    def t_run_form_lookup(self, form_id, lookup_id, value):
        from flows import intake

        f = self.form(form_id)
        lk = next((x for x in f.lookups or [] if x.get("id") == lookup_id), None)
        if lk is None:
            raise ToolError(f"No in-form bot '{lookup_id}' on this form (see get_form).")
        return intake.run_lookup(f, lk, value)

    def t_submit_form(self, form_id, values):
        from flows import intake
        from flows.conditions import evaluate
        from flows.views.fill import validate

        f = self.form(form_id)
        by_key = {}
        lowered = {str(k).strip().lower(): v for k, v in (values or {}).items()}
        for fld in f.input_fields():
            for cand in (fld["id"], fld.get("name"), fld.get("label")):
                if cand and str(cand).strip().lower() in lowered:
                    by_key[fld["id"]] = lowered[str(cand).strip().lower()]
                    break
        intake.rerun_lookups(f, by_key)
        intake.compute_formulas(f, by_key)
        flat = intake.flat_by_name(f, by_key)
        errors = validate(f.as_doc(), by_key, intake.editable_ids(f) - {x["id"] for x in f.fields if x.get("type") == "attachment"}, flat)
        if errors:
            names = {x["id"]: x.get("name") or x.get("label") for x in f.fields}
            return {"submitted": False, "errors": {names.get(k, k): v for k, v in errors.items()}}
        by_key = {k: v for k, v in by_key.items() if evaluate(next((x.get("condition") for x in f.fields if x["id"] == k), None), flat)}
        sub = intake.submit(f, by_key, {}, user=self.user)
        out = {"submitted": True, "submission_id": sub.pk, "status": sub.status, "error": sub.error or None,
               "filled_by_bots": {k: v for k, v in by_key.items() if k in intake.lookup_targets(f)}}
        if sub.packet:
            out["document"] = self._doc_summary(sub.packet)
        return out

    def t_list_team(self):
        from accounts.models import Group, Membership

        return {"teammates": [{"email": m.user.email, "name": m.user.get_full_name(), "role": m.role}
                              for m in Membership.objects.filter(workspace=self.ws, active=True).select_related("user")],
                "groups": [{"name": g.name, "members": [m.user.email for m in g.members.select_related("user")]}
                           for g in Group.objects.filter(workspace=self.ws)]}

    def t_create_group(self, name, member_emails=None):
        from accounts.models import Group, Membership

        self.need("can_manage", "manage groups")
        g, created = Group.objects.get_or_create(workspace=self.ws, name=name.strip()[:120])
        ms = Membership.objects.filter(workspace=self.ws, user__email__in=member_emails or [])
        g.members.add(*ms)
        missing = sorted(set(member_emails or []) - {m.user.email for m in ms})
        return {"group": g.name, "created": created, "members": [m.user.email for m in g.members.select_related("user")],
                "not_teammates": missing or None}

    def t_list_connectors(self):
        from flows.models import Connector

        return [{"id": c.pk, "name": c.name, "kind": c.kind, "base_url": c.base_url, "database": c.database, "active": c.active,
                 "auth": c.auth_type,
                 "actions": [{"key": a.get("key"), "name": a.get("name"), "method": a.get("method"), "path": a.get("path"), "sql": a.get("sql"),
                              "maps_to": [m.get("field") for m in a.get("map") or []]} for a in c.actions or []]}
                for c in Connector.objects.filter(workspace=self.ws)]

    def t_run_connector(self, connector_id, action, values=None, value=None):
        from flows import connectors
        from flows.models import Connector

        c = Connector.objects.filter(workspace=self.ws, pk=connector_id, active=True).first()
        if c is None:
            raise ToolError(f"No active connector with id {connector_id}.")
        try:
            res = connectors.run(c, action, values or {}, value=value)
        except connectors.ConnectorError as exc:
            raise ToolError(str(exc))
        return {k: res[k] for k in ("ok", "status", "error", "mapped", "data")}

    def t_ask_document(self, document_id, question):
        from flows import ai_actions

        p = self.packet(document_id)
        try:
            return {"answer": ai_actions.ask(p, question)}
        except ai_actions.AIError as exc:
            raise ToolError(str(exc))

    def t_check_workflow(self, workflow_id):
        from flows import checks

        found = checks.run(self.wf(workflow_id))
        return {"summary": checks.summary(found), "findings": [{k: f[k] for k in ("level", "code", "message")} for f in found]}

    def t_explain_workflow(self, workflow_id):
        from flows import ai_actions, ai_workflow

        wf = self.wf(workflow_id)
        try:
            ex = ai_workflow.explain(wf, self.user)
        except ai_actions.AIError as exc:
            raise ToolError(str(exc))
        return {"text": ai_workflow.explain_text(ex, wf.name), **{k: ex[k] for k in ("summary", "steps", "decisions", "automatic", "deadlines")}}

    def t_workflow_activity(self, workflow_id):
        from flows.views.builder import step_activity

        wf = self.wf(workflow_id)
        live = step_activity(wf)
        return {"steps": [{"step": s.name, **live.get(s.key, {"waiting": 0, "overdue": 0, "typical": ""})}
                          for s in wf.ordered_steps() if s.kind == "recipient"]}

    def t_list_key_dates(self, days=90):
        import datetime as dt

        from django.utils import timezone

        from flows.models import KeyDate, Packet

        today = timezone.localdate()
        visible = Packet.objects.visible_to(self.m)
        rows = KeyDate.objects.filter(workspace=self.ws, packet__in=visible, done_at__isnull=True, date__gte=today,
                                      date__lte=today + dt.timedelta(days=max(1, min(int(days or 90), 3650)))).select_related("packet")[:200]
        return {"key_dates": [{"date": k.date.isoformat(), "label": k.label, "document_id": k.packet_id,
                               "document": k.packet.name, "days_left": (k.date - today).days} for k in rows]}

    def t_search_documents(self, query):
        from flows import semantic

        res = semantic.answer(self.m, str(query)[:300], user=self.user)
        return {"answer": res["answer"], "documents": [{"id": h["packet"].pk, "name": h["packet"].name,
                                                        "workflow": h["packet"].workflow.name, "status": h["packet"].status,
                                                        "snippet": h["snippet"]} for h in res["hits"]]}

    def t_list_spreadsheets(self):
        from flows.models import DataSheet

        return [{"name": s.name, "columns": s.columns, "rows": s.rows.count()} for s in DataSheet.objects.filter(workspace=self.ws)]

    def t_get_spreadsheet(self, name, limit=50):
        from flows.models import DataSheet

        s = DataSheet.objects.filter(workspace=self.ws, name=name).first()
        if s is None:
            raise ToolError(f"No spreadsheet named '{name}'.")
        return {"name": s.name, "columns": s.columns, "rows": [r.values for r in s.rows.order_by("id")[: max(1, min(int(limit or 50), 500))]]}

    def t_create_spreadsheet(self, name, columns, rows=None):
        from flows.models import DataSheet, DataSheetRow

        self.need("can_create", "create spreadsheets")
        s, created = DataSheet.objects.get_or_create(workspace=self.ws, name=name.strip()[:160], defaults={"columns": columns})
        if not created:
            s.columns = list(dict.fromkeys([*(s.columns or []), *columns]))
            s.save(update_fields=["columns"])
        DataSheetRow.objects.bulk_create([DataSheetRow(sheet=s, values={c: r.get(c, "") for c in s.columns}) for r in rows or []])
        return {"name": s.name, "created": created, "columns": s.columns, "rows": s.rows.count()}

    # ---- JSON-RPC --------------------------------------------------------------------------------
    def handle(self, msg):
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:  # notification (e.g. notifications/initialized)
            return None
        try:
            if method == "initialize":
                v = params.get("protocolVersion")
                result = {"protocolVersion": v if v in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "paperlane", "title": "Paperlane", "version": "1.0.0"},
                          "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                allowed = set(self.tool_names())
                result = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, d, s in TOOLS if n in allowed]}
            elif method == "tools/call":
                result = self.call(params.get("name"), params.get("arguments") or {})
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        except Exception as exc:  # never let one bad request kill the server
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}}

    def run(self, name, args):
        """Call a tool and return its Python result (raises ToolError) — used by the REST API."""
        if name not in self.tool_names():
            raise ToolError(f"This API token is read-only; {name} changes data." if name in {t[0] for t in TOOLS} else f"Unknown tool: {name}")
        try:
            return getattr(self, f"t_{name}")(**args)
        except TypeError as exc:
            raise ToolError(f"Bad arguments for {name}: {exc}")

    def call(self, name, args):
        fn = getattr(self, f"t_{name}", None)
        if fn is None or name not in {t[0] for t in TOOLS}:
            return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}
        if name not in self.tool_names():
            return {"content": [{"type": "text", "text": f"This API token is read-only; {name} changes data."}], "isError": True}
        try:
            out = fn(**args)
            return {"content": [{"type": "text", "text": json.dumps(out, indent=1, default=str)}], "isError": False}
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except TypeError as exc:
            return {"content": [{"type": "text", "text": f"Bad arguments for {name}: {exc}"}], "isError": True}
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            return {"content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}], "isError": True}
