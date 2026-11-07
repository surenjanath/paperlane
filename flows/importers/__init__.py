"""Workflow import/export (Paperlane exports and blueprints)."""
import json

from django.utils import timezone

FORMAT = "paperlane.workflow/1"


def export_workflow(wf):
    return {"format": FORMAT, "exported_at": timezone.now().isoformat(), "name": wf.name, "description": wf.description,
            "kind": wf.kind, "routing_mode": wf.routing_mode, "autostart": wf.autostart or {},
            "welcome_message": wf.welcome_message if wf.welcome_enabled else "", "snapshot": wf.export_snapshot()}


def detect(data):
    if isinstance(data, dict) and data.get("format") == FORMAT:
        return "paperlane"
    if isinstance(data, dict) and isinstance(data.get("steps"), list) and isinstance(data.get("documents"), list):
        return "blueprint"
    return None


def import_any(data, workspace, user):
    """Returns (workflow, report dict)."""
    from .. import services
    from ..models import Workflow

    kind = detect(data)
    if kind == "paperlane":
        wf = Workflow.objects.create(workspace=workspace, name=data.get("name") or "Imported workflow", created_by=user,
                                     description=data.get("description", ""), kind=data.get("kind", "workflow"),
                                     routing_mode=data.get("routing_mode", "linear"), autostart=data.get("autostart") or {},
                                     welcome_enabled=bool(data.get("welcome_message")), welcome_message=data.get("welcome_message", ""),
                                     source_ref="paperlane-export")
        services.load_snapshot(wf, data["snapshot"], fresh_keys=True)
        return wf, {"steps": [s["name"] for s in data["snapshot"].get("steps", [])],
                    "bots": [b["name"] for b in data["snapshot"].get("bots", [])], "groups": [], "documents": [], "warnings": []}
    if kind == "blueprint":
        from ..blueprint import BlueprintError, build_workflow

        try:
            wf, problems = build_workflow(workspace, user, data, publish=False, strict=False)
        except BlueprintError as exc:
            raise ValueError(str(exc))
        wf.source_ref = "blueprint"
        wf.save(update_fields=["source_ref"])
        return wf, {"steps": list(wf.steps.order_by("order").values_list("name", flat=True)),
                    "bots": list(wf.bots.values_list("name", flat=True)), "groups": [],
                    "documents": list(wf.documents.values_list("name", flat=True)), "warnings": problems}
    raise ValueError("Unrecognised file: expected a Paperlane workflow export or a workflow blueprint.")


def load_file(fileobj):
    return json.loads(fileobj.read().decode("utf-8-sig"))
