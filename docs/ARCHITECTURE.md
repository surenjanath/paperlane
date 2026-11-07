# Paperlane architecture

Paperlane is a single Django project: server-rendered templates with small amounts of plain JavaScript and no
front-end build step. SQLite is the default database, and any Django-supported database works.

## Apps

| App | Responsibility |
|---|---|
| `paperlane/` | Settings, root URLs, WSGI. |
| `accounts/` | Workspaces, memberships and roles, groups, invitations, profiles (two-step verification, out of office), contacts, connected accounts, email templates, usage, API tokens; sign-in, onboarding, team and workspace settings views. |
| `flows/` | The product core: workflows, steps, documents and forms, bots, packets ("documents" in the UI), step runs, revisions, audit, start forms, connectors, webhooks; the routing engine, bot executor, mailer, AI, MCP and REST API. |
| `core/` | Dashboard and analytics widgets, inbox, search, notifications, archive and trash, template tags (`ui`), HTML sanitizer, shared helpers. |
| `superadmin/` | Platform console (`/platform/`): platform settings and feature switches, workspace and user management, sign-in log, broadcasts, background jobs, system health; middleware that enforces maintenance mode, suspension, idle sign-out and required two-step verification. |

## Core model

```
Workspace ─┬─ Membership (user + role: owner / supervisor / auditor / creator / filler / guest)
           ├─ Group ── members (Membership)
           ├─ Workflow ─┬─ Step (recipient / bot / branches, with lanes and jumps)
           │            ├─ WorkflowDocument (schema = fields, design)
           │            ├─ Bot (type, trigger, step, config, conditions)
           │            └─ WorkflowVersion (published snapshots)
           ├─ Packet ("document") ─┬─ StepRun (one per recipient/step, token link, claim, deadline)
           │   definition = the    ├─ PacketRevision, PacketFile, Comment
           │   published snapshot  └─ AuditEvent, BotLog, BotJob
           ├─ IntakeForm (public start form) ── FormSubmission
           ├─ Connector (custom HTTP/JSON API + actions)
           └─ WebhookEndpoint ── WebhookDelivery
```

A packet freezes the workflow's **published snapshot** when it starts, so publishing a new version never changes
documents already in flight. Live, non-versioned settings (for example `Workflow.options["duplicates"]`) are read
from the workflow itself.

## Request flow for a document

1. **Start**: manual run, public link, start form, webhook, schedule, another workflow, or a bot.
   `engine.start_packet` copies the snapshot, applies the prefill, runs the duplicate check and `flow_start`
   bots, then calls `advance`.
2. **Routing**: `engine.advance` walks the diagram (conditions, branch lanes, jumps) and creates a `StepRun`
   for each recipient. The deadline comes from `sla.py` and out-of-office delegation from `sla.apply_delegation`.
3. **Filling**: `/f/<token>/` (`flows/views/fill.py`). Group steps can be claimed. Values are validated per
   field type (`fieldtypes.py`) and formulas recalculated.
4. **Completion**: `engine.complete_run` stores values, runs `step_complete` bots and routes on.
   `complete_packet` runs `flow_complete` bots.
5. **Background**: `bots.tick()` handles reminders, access expiry, scheduled starts, delayed bots and deadline
   escalation (`sla.process_overdue`); `webhooks.deliver_due()` handles retries. It runs in a thread inside the
   web process (`PAPERLANE_SCHEDULER=1`) or via `manage.py paperlane_tick --loop 30`.

## Conditions

`flows/conditions.py` evaluates rule groups (`and`/`or`) against `packet.flat_values()` plus a context from
`engine.condition_context`:
- `@user.*`, `@prev.status`, `@revision`, `@tag`;
- `@date`, `@time`, `@day`, `@month`;
- `@filled:<doc>`, `@all_fillable:<doc>`;
- `@duplicate`, `@step`, `@status`.

Multi-field rules use `@any:A,B` and `@all:A,B`, and rule values can reference other fields with `{field:Name}`.

## Bots

`flows/bots.py` holds the catalog (`CATALOG`: name, category, settings fields, default trigger) and one handler
`h_<type>` per bot.
- **Execution:** `run_bot` evaluates the bot's conditions, applies its delay (creating a `BotJob`), runs the
  handler, and writes a `BotLog`. A failing bot never stops a document.
- **Helpers:** `token_render` (tokens like `{field:X}` and `{today}`), `set_field`, `parse_mapping`.
- **Integrations:** "Save to …" and "Export to …" bots deliver to a connected account's endpoint. Connectors
  (`flows/connectors.py`) call any configured API.

## AI

- **Providers** (`flows/ai_providers.py`): Ollama, which is auto-discovered and can use a per-workspace address
  through a contextvar; OpenAI-compatible servers; Claude; and a built-in rule-based planner.
- **AI inside workflows** (`flows/ai_actions.py`): fill fields, summarize, classify, and Ask AI.
- **Describe it** (`flows/ai_builder.py`) turns a description into a workflow blueprint (`flows/blueprint.py`).

## Interfaces

- **Web UI**: Django templates in `templates/`, CSS in `static/css/app.css`, JavaScript in `static/js/`.
- **REST API**: `/api/v1/` (`flows/views/api.py`), with bearer API tokens (`accounts.ApiToken`).
- **MCP**: `flows/mcp.py`, served over stdio (`manage.py paperlane_mcp`) and HTTP (`POST /mcp`).
- **Webhooks**: outgoing signed events (`flows/webhooks.py`); incoming autostart at `/hooks/<token>/`.

## Security notes

- Permissions:
  - Roles are checked in `flows/permissions.py` (`get_workflow`, `get_packet`, `require`).
  - Visibility uses `Workflow.objects.visible_to` and `Packet.objects.visible_to`, which include group assignments.
- Recipients:
  - Fill links are unguessable tokens.
  - A group step taken by one member is blocked for the others.
- Anything that changes data requires POST with CSRF protection. A `next=` redirect is only followed when it
  points at this site (`core/utils.py`).
- Untrusted content:
  - Author-written HTML blocks are sanitized (`core/sanitize.py`).
  - JSON is embedded with `json_script` or the escaping `jsonify` filter.
- Outgoing calls:
  - Platform switches can block calls to private networks (`superadmin/netguard.py`).
  - Public form lookups are rate-limited.
- Sign-in:
  - Throttling, two-step verification (TOTP plus recovery codes), and an active sessions list.
  - The platform can require two-step verification and sign out idle users.
