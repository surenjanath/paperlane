from functools import wraps

from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404

from .models import Packet, Workflow


def require(capability):
    """View decorator: membership must have ``can_<capability>``."""

    def deco(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            m = request.membership
            if m is None or not getattr(m, f"can_{capability}"):
                raise PermissionDenied(f"Your workspace role cannot {capability}.")
            return view(request, *args, **kwargs)

        return wrapped

    return deco


def can_edit_workflow(membership, workflow):
    if membership.can_manage:
        return True
    return membership.can_create and (workflow.created_by_id == membership.user_id or workflow.access == Workflow.TEAM
                                      or workflow.shared_users.filter(pk=membership.user_id).exists())


def get_workflow(request, pk, *, edit=False, include_deleted=False):
    qs = Workflow.objects.visible_to(request.membership)
    if not include_deleted:
        qs = qs.filter(trashed_at__isnull=True)
    wf = get_object_or_404(qs, pk=pk)
    if edit and not can_edit_workflow(request.membership, wf):
        raise PermissionDenied("You don't have permission to edit this workflow.")
    return wf


def get_packet(request, pk, *, manage=False):
    packet = get_object_or_404(Packet.objects.visible_to(request.membership).select_related("workflow"), pk=pk)
    if manage:
        m = request.membership
        if not (m.can_manage or packet.created_by_id == m.user_id or packet.workflow.created_by_id == m.user_id
                or (m.can_create and can_edit_workflow(m, packet.workflow))):
            raise PermissionDenied("You can't manage this document.")
    return packet
