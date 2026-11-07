"""Shared queries so the inbox list, its tabs and the sidebar badge always agree."""
from django.db.models import Q


def my_runs(user, workspace):
    """Step runs addressed to ``user`` (directly, by email or through a group) on live documents."""
    from flows.models import StepRun

    mine = Q(assignee_user=user) | Q(assignee_email__iexact=user.email) | Q(assignee_group__members__user=user)
    covering = covering_for(user)  # teammates who are out of office and delegated their group work to this user
    if covering:
        mine |= Q(assignee_group__members__user__in=covering, status__in=["pending", "opened"])
    return (StepRun.objects.filter(packet__workspace=workspace, packet__trashed_at__isnull=True,
                                   packet__archived_at__isnull=True, packet__is_test=False,
                                   packet__workflow__trashed_at__isnull=True)
            .filter(mine)
            .exclude(Q(claimed_by__isnull=False) & ~Q(claimed_by=user) & Q(status__in=["pending", "opened"]))  # taken by a teammate
            .distinct())


def waiting_packet_count(user, workspace):
    """Documents with a step waiting for ``user`` (what the Inbox badge shows)."""
    from flows.models import Packet

    return (my_runs(user, workspace).filter(status__in=["pending", "opened"], packet__status=Packet.IN_PROGRESS)
            .values("packet_id").distinct().count())


def covering_for(user):
    """Users who are out of office today and have chosen ``user`` as their delegate."""
    from accounts.models import Profile

    from django.db.models import Q
    from django.utils import timezone

    today = timezone.localdate()
    return list(Profile.objects.filter(Q(ooo_from__isnull=True) | Q(ooo_from__lte=today), ooo_delegate=user,
                                       ooo_until__gte=today).values_list("user_id", flat=True))
