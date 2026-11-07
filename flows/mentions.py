"""@mentions in document comments: @email, @username or @"First Last" → notification + email."""
import re

MENTION = re.compile(r'@"([^"]{2,80})"|@([\w.+-]+@[\w-]+(?:\.[\w-]+)+)|@([\w.]{2,40})')


def members(workspace):
    from accounts.models import Membership

    return [m.user for m in Membership.objects.filter(workspace=workspace, active=True).select_related("user")]


def resolve(text, workspace):
    """Users mentioned in ``text`` (members of the workspace only)."""
    people = members(workspace)
    found = []
    for quoted, email, word in MENTION.findall(text or ""):
        cands = []
        if email:
            cands = [u for u in people if (u.email or "").lower() == email.lower()]
        elif quoted:
            cands = [u for u in people if (u.get_full_name() or "").lower() == quoted.strip().lower()]
        else:
            w = word.rstrip(".").lower()
            cands = [u for u in people if u.username.lower() == w] or \
                    [u for u in people if (u.first_name or "").lower() == w]
            if len(cands) > 1:
                cands = []  # ambiguous first name — needs the full name or email
        for u in cands[:1]:
            if u not in found:
                found.append(u)
    return found


def notify(packet, author, text, link):
    from . import engine, mailer

    mentioned = [u for u in resolve(text, packet.workspace) if u != author]
    who = engine.actor_label(author)
    for u in mentioned:
        engine.notify(u, packet.workspace, f"{who} mentioned you on {packet.name}", text[:200], link, kind="assigned")
        if u.email:
            mailer.send(packet.workspace, u.email, "custom", {
                "subject": f"{who} mentioned you on {packet.name}",
                "message": f"{who} wrote:\n\n{text}\n\nOpen the document: {engine.absolute(link)}"}, packet=packet)
    return mentioned
