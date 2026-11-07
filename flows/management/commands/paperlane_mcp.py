"""Paperlane MCP server over stdio.

    python manage.py paperlane_mcp --workspace demo --user owner

Register with Claude Code (project scope, see .mcp.json) or:
    claude mcp add paperlane -- <venv>/bin/python <project>/manage.py paperlane_mcp --workspace demo --user owner

Protocol: JSON-RPC 2.0, one message per line on stdin/stdout (MCP stdio transport). The tools live in flows/mcp.py.
"""
import json
import sys

from django.db import close_old_connections

from django.core.management.base import BaseCommand, CommandError

from flows.mcp import TOOLS, Server, ToolError  # noqa: F401  (re-exported for callers and tests)


class Command(BaseCommand):
    help = "Run the Paperlane MCP server on stdio (for Claude and other MCP clients)."

    def add_arguments(self, parser):
        parser.add_argument("--workspace", default="demo", help="Workspace slug")
        parser.add_argument("--user", default="owner", help="Username or email the server acts as")

    def handle(self, *args, **o):
        from django.contrib.auth import get_user_model
        from django.db.models import Q

        from accounts.models import Workspace

        ws = Workspace.objects.filter(slug=o["workspace"]).first()
        user = get_user_model().objects.filter(Q(username=o["user"]) | Q(email__iexact=o["user"])).first()
        if ws is None or user is None:
            raise CommandError("Unknown --workspace or --user.")
        server = Server(ws, user)
        print(f"Paperlane MCP ready: workspace={ws.slug} user={user.email}", file=sys.stderr, flush=True)
        for line in sys.stdin:
            close_old_connections()  # a long-lived process: drop a dead or stale database connection between requests
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            else:
                out = [r for r in (server.handle(m) for m in msg) if r] if isinstance(msg, list) else server.handle(msg)
            if out:
                sys.stdout.write(json.dumps(out, default=str) + "\n")
                sys.stdout.flush()
