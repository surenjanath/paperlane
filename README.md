# Paperlane

**Document-workflow automation**: visual workflow builder, drag-and-drop forms, e-signatures, bots, start forms,
team work queues, SLAs, AI (local Ollama or cloud) and a super admin console. It is built with Python and Django,
with server-rendered templates and no front-end build step.

Paperlane also has group work queues, duplicate detection, custom API connectors, AI bots, an MCP server for AI
agents, and platform administration.

**See it on a real process:** [examples/bank-loan](examples/bank-loan/) is a complete personal-loan workflow connected
to a sample core-banking database: customer lookup on the public form, an automatic credit check, officer review,
compliance and credit committee for larger or riskier loans, an offer letter the client signs after an email code,
and disbursement. Run `python manage.py seed_bank_demo` after `seed_demo`.

| | |
|---|---|
| **Stack** | Python 3.12+ · Django 6 · SQLite (any Django database) · vanilla JS · xhtml2pdf · openpyxl · segno |
| **Docs** | [Architecture](docs/ARCHITECTURE.md) · [Deployment](docs/DEPLOYMENT.md) |
| **Tests** | `python manage.py test` (about 150 tests, including a sweep of every route for every role) |

## Contents

- [Quick start](#quick-start)
- [Configuration](#configuration-environment-variables)
- [Feature map](#feature-map)
- [AI builder (MCP server)](#ai-builder-mcp-server)
- [Deadlines, out of office, insights and webhooks](#deadlines-out-of-office-insights-and-webhooks)
- [Describe it (AI workflow builder)](#describe-it-ai-workflow-builder)
- [Work queues, duplicates, mentions and automation](#work-queues-duplicates-mentions-and-automation)
- [Super admin console](#super-admin-console-platform)
- [Custom connectors and AI in workflows](#custom-connectors-and-ai-in-workflows)
- [Partial withdrawal start form](#partial-withdrawal-start-form)
- [Security and API access](#security-and-api-access)
- [Sign-in and public pages](#sign-in-and-public-pages)
- [Importing workflows](#importing-workflows)
- [Project layout](#project-layout)

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py seed_demo          # optional demo workspace
.venv/bin/python manage.py seed_stories       # optional: five workflows with documents at every stage
.venv/bin/python manage.py seed_bank_demo     # optional: the bank loan example (examples/bank-loan)
.venv/bin/python manage.py runserver
```

Open http://127.0.0.1:8000. Demo login (local test data created by `seed_demo`):

| user | email | role |
|------|-------|------|
| owner | owner@paperlane.test | Workspace Owner (also Django admin) |
| sam | sam@paperlane.test | Supervisor |
| ana | ana@paperlane.test | Auditor |
| cal | cal@paperlane.test | Creator |
| fay | fay@paperlane.test | Filler |

Password for all demo users: `paperlane-demo-2026` (override with `PAPERLANE_DEMO_PASSWORD` before seeding).

`seed_stories` adds five workflows (Expense claim, Leave request, Vendor onboarding, Insurance claim, Purchase
request) and 27 documents filled step by step by named teammates over the last three weeks: Priya Patel (Finance),
Marcus Lee (HR), Dana Cruz (Claims), Leo Brooks (Procurement), Tom Reyes and Grace Kim, plus outside clients and
vendors. Some are finished, some wait on a person or a group queue, one is sent back and one is declined. To see a
request move, sign in as **Fay**, complete *Purchase – Ergonomic chair*, then sign in as **Sam** and approve it under
**Approvals**; it lands in the Procurement queue. Rebuild with `seed_stories --replace`.

Run the scheduler (reminders, access expirations, scheduled autostarts) alongside the server:

```bash
.venv/bin/python manage.py paperlane_tick --loop 60
```

Run the tests: `.venv/bin/python manage.py test`

## Configuration (environment variables)

Copy [`.env.example`](.env.example) and load it before starting (`set -a; source .env; set +a`). Every value is
optional for local development.

| Variable | Default | Purpose |
|----------|---------|---------|
| `PAPERLANE_SECRET_KEY` | dev key | Django secret key. Set it in production. |
| `PAPERLANE_DEBUG` | `1` | Set `0` in production. |
| `PAPERLANE_ALLOWED_HOSTS` | localhost… | Comma separated. |
| `PAPERLANE_SITE_URL` | `http://127.0.0.1:8000` | Absolute links in emails. |
| `PAPERLANE_HTTPS` | `0` | `1` turns on secure cookies, HSTS (`PAPERLANE_HSTS_SECONDS`), HTTPS redirect and the proxy header. |
| `PAPERLANE_TIME_ZONE` | `America/Port_of_Spain` | Display and schedule time zone. |
| `PAPERLANE_EMAIL_BACKEND` | console | e.g. `django.core.mail.backends.smtp.EmailBackend` |
| `PAPERLANE_EMAIL_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_TLS` | localhost / 25 / – / – / 0 | SMTP server. |
| `PAPERLANE_FROM_EMAIL` | `Paperlane <no-reply@paperlane.local>` | Sender address. The display name can be changed per workspace. |
| `PAPERLANE_SCHEDULER` | `1` | Run background jobs in the web process. Set `0` and run `manage.py paperlane_tick --loop 30` instead. |
| `PAPERLANE_SCHEDULER_SECONDS` | `30` | How often the background jobs run. |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Local AI models (auto-discovered). Each workspace can override it. |
| `PAPERLANE_OLLAMA_MODELS` | all | Comma list limiting which Ollama models are offered. |
| `PAPERLANE_AI_ENGINE` | auto | Default engine, e.g. `ollama:qwen3.5:9b`. |
| `PAPERLANE_AI_TIMEOUT` | `300` | Seconds to wait for a local model. |
| `ANTHROPIC_API_KEY` / `PAPERLANE_AI_MODEL` | – | Claude. |
| `PAPERLANE_OPENAI_BASE_URL` / `PAPERLANE_OPENAI_API_KEY` | – | Any OpenAI-compatible server. |
| `PAPERLANE_DEMO_PASSWORD` | `paperlane-demo-2026` | Password for the users created by `seed_demo`. |

Every email is also stored in **My workspace › Email log**, so the app is fully usable without SMTP.
Production setup is covered in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Feature map

**Shell** – icon rail (⌘K search across workflows/templates/documents/contacts, team, contacts, help,
notifications panel, workspace, profile menu), sidebar with workspace switcher, Create menu
(Document / Template / Workflow), Dashboard, Inbox, Documents, Templates, Workflows, Recents, Archive, Trash, plan.

**Dashboard** – Home (create cards + 6 ready-made automations that build complete workflows) and
Analytics (configurable Chart.js widgets: metrics, workflows, range, day/week/month, area/line/bar, fullscreen).

**Inbox** – All / Waiting for me / Completed, search.

**Audit trail** (rail, owners/supervisors/auditors) – workspace-wide day-grouped timeline, date range, CSV export.

**Documents** – quick filters (Needs attention, No activity 7+ days, My documents), filters (status, workflow,
tag, dates), sort, search, pagination, bulk download (ZIP of PDFs) / export metadata / tags / archive / delete.
Row & detail actions: check status (step diagram), documents data, revisions (view + PDF per revision),
revise (send back to a step), download PDF (with certificate of completion & audit trail), printable view,
export metadata / audit CSV, save and send (email a PDF), comments, files, lock/unlock, rename, cancel,
remind / resend / reassign / copy link per recipient, audit trail, bots log, emails, archive, trash, restore.

**Workflows & Templates** – card library grouped by recency, search, show hidden, show mine/shared, tag filter,
sort; card menu (tags, duplicate, archive, trash, hide/unhide, copy link). Workflow page: run, test,
distribute via link / bulk CSV / webhook API, manage access (private/team/people/groups), bots log, audit trail,
consent to eSign, welcome message, rename, version history with restore, duplicate, archive, trash.

**Conditions** (steps, branches, documents, bots) – the live condition types: document field (is / is not / contains / regex /
empty / not empty / numeric / dates after & until, or *all fillable* / *all required* fields), recipient (email, first/last name,
position, access level), date (date, time, date & time, weekday, month), previous step status, revision number, tag,
number of filled fields.

**Autostart** – several per workflow: scheduled start (once / every N minutes·hours·days·weeks / on schedule by weekdays,
month days, every day or custom months; start date, timezone, stop never / on date / after N runs, notify owner),
scheduled start from an Excel (XLSX) or CSV file (one document per row, or the next row per run), from another
workflow, from external event (also the integration sources, via the webhook); shareable link tab.

**Branches** – a Branches node splits the diagram into named lanes drawn side by side; each
lane has rules, its own steps (nested branches allowed) and an exit: continue after the branches, complete, or go to
any step/branch (loops). A lane can also jump straight to a step or complete without steps.

**Example** – `python manage.py build_claims_workflow [--replace]` builds and publishes “Insurance Claim” (public link
intake → Route by amount lanes → Decision lanes with a more-information loop → Finance) and prints its start link.

**Jumps** – a step can carry conditional “jump from this step” links; a jump skips the target's own
conditions. **Multi-field rules** – `@any:FieldA,FieldB` matches when any listed field matches. `@all:FieldA,FieldB` matches when every listed field matches.

**Forms** (sidebar › Forms) – custom start forms that replace “web form → spreadsheet → workflow” chains. Build fields in the
same drag-and-drop editor (conditions, formulas, uploads, signatures, design), pick the workflow it starts, and share a
public link or embed it (e.g. a WordPress Custom HTML block). **In-form bots** run while the form is being filled: when a
trigger field (e.g. Policy number) is filled they look the value up in a Paperlane spreadsheet, earlier documents of a
workflow or an external API and pre-fill other fields (re-checked on the server at submit; public lookups are
rate-limited and return only mapped fields). On submit: values map to the workflow by field name (or an explicit
mapping), files are attached, the first step can go to the email typed in the form or count as completed (“the form is
the first step”), and submissions can also be logged to a spreadsheet. Ready-made templates (claim intake, beneficiary change,
change of address, surrender, service request, employee details) and a demo “Policies” sheet for lookups.

Rule values can reference another field with `{field:Name}`.

**Builder** – edit lock ("The process is in use" + send request + take over), Start node with autostart
(schedule hourly/daily/weekly/monthly, from another workflow with field pre-fill, external webhook),
recipient steps, branch steps (rules → jump to step / complete), bot steps, move/duplicate/rename/delete,
zoom, documents panel (new form, upload/drag & drop file, conditional documents, rename, copy, reorder, remove),
Finish later / Test / Publish (validation + versioning), **Checks** (`flows/checks.py`: broken jumps/branch targets/bot
steps, rules and `{field:X}` tokens on missing fields, lanes that can never run, empty groups; errors block Publish,
warnings mark nodes and feed the AI review), **undo/redo** of every builder change (`flows/undo.py`, `BuilderSnapshot`,
last 30, ⌘Z / ⇧⌘Z), step finder (`/`), fit to screen (`F`), ⌘/Ctrl + scroll zoom, blueprint export. Step drawer: recipient (email/phone incl. `{field:X}`,
current user, contact, group, anyone with link), subject & message with preview, attach PDF, per-document access
(fill / view / hidden), field reassignment, revoke on completion, thank-you text, conditions,
redirect URL, custom tags, reminders, access expiration, notification emails,
forbid decline / discard / draft, recurrence.

**Bots** – triggers: workflow start, step sent (pre-fill), step opened, step complete, workflow complete, inline bot step;
per-bot conditions; catalog with categories and search. Implemented bots: rename, save PDF to storage,
lock, archive, certificate, email on completion/opening, custom email, fax via email gateway, SMS via gateway,
reminders, tags, pre-fill (custom values, doc-to-doc, another workflow, CSV, spreadsheet, API), export to
spreadsheet, auto-fill signature, webhook — plus "Save to …" / "Export to …" bots for ~40 services that deliver
to the endpoint of a **Connected account** (e.g. a Zapier/Make/n8n hook or your own API) and keep a local copy.

**Document editor** – Elements (21) and Widgets (12) by click or drag-and-drop, drag reorder, pages, half-width two-column layout, disabled / hidden for recipients, date formats, checkbox groups with option keys, multi-file attachment requests with limits, roles
(color-coded, add new role → new step), properties (label, variable name, placeholder, help, required, default,
options, rows/columns, sub-fields, formula builder, min/max, max length, regex), field conditions,
design (width, font, logo, header/accent/background colors, set as default), undo/redo, preview,
rule-based form assistant, unsaved-changes guard.

**Recipient experience** – branded fill page, multi-page forms, conditional fields, live formulas,
signature/initials pad (draw, type, use saved), file uploads, tables / repeatable groups, matrix, rating,
image choice, video embeds, eSign consent, save draft, discard, decline with reason, welcome message,
thank-you page (workspace + step overrides, next document shortcut, recipient registration), expired/closed screens,
public link start page.

**Team & contacts** – teammates table (filters, search, sort), invite by email (role), change role,
block/activate, remove, groups (create/edit/delete, membership, bulk add), pending invitations,
role legend; contacts add/edit/delete/bulk delete/import CSV/export CSV/search/sort.

**My workspace** – workspace info (+ logo, address, leave), settings toggles, theme (live preview),
email templates per event (edit, reset, preview), thank-you page, domains, spreadsheets,
email log, subscription credits, billing, usage log (date range, CSV).
**My account** – profile (avatar, default signature/initials), email, password, language, delete account,
notification settings, connected accounts, my activity.

## AI builder (MCP server)

Paperlane ships an MCP server so Claude (or any MCP client) can build complete workflows — documents and fields,
steps and recipients, nested branch lanes, jumps, conditions, bots, autostarts and start forms with in-form bots —
then publish, start and step through them.

```bash
python manage.py paperlane_mcp --workspace demo --user owner     # JSON-RPC 2.0 over stdio
```

`.mcp.json` in the project root registers it for Claude Code (project scope) — open Claude Code here and approve the
`paperlane` server. The server acts as the given user, with that user's role permissions.

Tools: `get_build_guide`, `list_workflows`, `get_workflow` (exports a blueprint), `build_workflow` (blueprint →
workflow, `publish`, `replace`), `publish_workflow`, `trash_workflow`, `start_workflow`, `list_documents`,
`get_document`, `complete_step`, `list_form_templates`, `list_forms`, `get_form`, `build_form`, `publish_form`,
`run_form_lookup`, `submit_form`, `list_team`, `create_group`, `list_spreadsheets`, `get_spreadsheet`,
`create_spreadsheet`. The blueprint format is documented in `flows/blueprint.py` and returned by `get_build_guide`;
`core/test_deep.py` (`PURCHASE_BLUEPRINT`) is a full worked example.

## Deadlines, out of office, insights and webhooks

- **Deadlines (SLA)** — builder › step › Advanced settings › Deadline: complete within N hours / days / business days.
  Builder cards show “⏱ Due in …”; documents show *Due* / *Overdue* pills and the Documents page has an **Overdue**
  filter. When a deadline passes: the holder and workflow owner are told, an optional tag is added, and escalation
  either notifies a person/group or **reassigns** the step (the new holder gets a fresh deadline). Blueprint:
  `settings.sla = {amount, unit, escalate, escalate_to, tag}`; the planner understands “approves within 2 business days”.
- **Out of office** — My account › Out of office: dates + a teammate who covers. New steps for you go to them (audited
  as `step.delegated`, shown as “covering for …”), your share of group steps appears in their Inbox and they can
  complete it. A banner reminds you while it's on.
- **Insights** — workflow page › ⋯ › Insights: per-step median/average time, work waiting now and its age, overdue,
  on-time rate, declines; cycle time, completion rate, started-vs-completed per week, and who holds open work.
- **Webhooks** — My workspace › Webhooks: choose events (document.started, step.sent/completed/declined/reassigned/
  overdue/delegated, document.completed/cancelled, form.submitted, comment.added), a workflow filter and optional
  field values. Requests carry `X-Paperlane-Event`, `-Delivery`, `-Timestamp` and `-Signature: sha256=HMAC(secret,
  "ts.body")`. Delivered right after the change; failures retry at 1 m, 5 m, 30 m, 2 h, 12 h; delivery log with
  payloads, Send test, Redeliver, Rotate secret, Pause.
- **Background jobs** run inside the server process every 30 s (reminders, deadlines, scheduled autostarts, webhook
  retries) — `PAPERLANE_SCHEDULER=0` to turn off and run `manage.py paperlane_tick --loop 30` instead;
  `PAPERLANE_SCHEDULER_SECONDS` changes the interval. The Webhooks page shows when it last ran.
- **Performance** — Inbox is paged (25) and prefetches row data; the builder loads recipients in one query; the
  Audit trail is paged (100) and searchable.

## Describe it (AI workflow builder)

Workflows › **Describe it** (also in the Create menu): write the process in plain words, pick an engine, review the
preview (steps, recipients, conditions, rejection branches, fields per step, bots), then **Create as draft** or
**Create and publish**. Nothing is created until you click Create; the blueprint JSON can be edited first.

Engines (`flows/ai_providers.py`, picked per user on the page):

| Engine | How to enable |
|---|---|
| Built-in planner | Always on: instant and offline, rule-based (`flows/ai_builder.py`) |
| **Ollama** (local) | Run Ollama; every installed chat model is listed. `OLLAMA_HOST` (default `http://127.0.0.1:11434`), `PAPERLANE_OLLAMA_MODELS=qwen3.5:9b,…` to limit the list |
| OpenAI-compatible (local) | LM Studio / llama.cpp / vLLM: `PAPERLANE_OPENAI_BASE_URL=http://127.0.0.1:1234/v1` (+ `PAPERLANE_OPENAI_API_KEY`) |
| Claude | `ANTHROPIC_API_KEY` (+ `PAPERLANE_AI_MODEL`, default `claude-opus-5-5`) |

`PAPERLANE_AI_ENGINE` sets the default (e.g. `ollama:qwen3.5:9b`), `PAPERLANE_AI_TIMEOUT` the wait for local models
(default 300 s). Model replies are cleaned up (field-type and operator synonyms, step names used as keys, missing ids,
`{field:…}` tokens that point nowhere), dry-run inside a rolled-back transaction, and sent back once for repair if
they don't build; if they still fail, the built-in planner is used and the page says so. Tested with qwen3.5:9b
(about 40 s for a 4-step workflow on this Mac).

## Work queues, duplicates, mentions and automation

- **Group work queue**:
  - On a group step, a member can **Take this task**. While they hold it, the rest of the group doesn't see it in their inbox and can't open it.
  - Saving a draft or completing the step takes it automatically.
  - The holder can **Release** it back to the group; supervisors and owners can **Take over**.
- **Team workload** (sidebar, for owners, supervisors and auditors):
  - Per person: open steps, overdue steps, oldest item, steps done in the last 7 days and average time per step.
  - Per group queue: how much is waiting.
  - Filters: overdue, not taken yet, by person or by queue.
  - Managers can bulk-reassign or release the selected steps.
- **Duplicate check** (workflow page › ⋯ › Duplicate check): compare up to 5 fields (e.g. Policy Number) with documents still in progress, or with documents from the last N days. A match gets:
  - the tag **#Possible duplicate**;
  - a comment linking the other document;
  - a warning banner on the document and fill pages;
  - a notification to the workflow owner.

  Rules can test `@duplicate` (yes/no).
- **@mentions** in comments: typing `@` suggests teammates. Mentioned people get a notification and an email, and the mention is highlighted.
- **New bots**:

  | Bot | What it does |
  |---|---|
  | Generate a reference number | Sequential references, e.g. `WD-{yyyy}-{seq:5}`; can restart each year; one per document; can be added to the document name. |
  | Assign to the next team member | Least open work, or taking turns; can skip people who are away. |
  | Transform or combine fields | Case, trim, digits only, combine text, date + days or business days, round, today. |
  | Start another workflow | Chains processes and copies values across; has a loop guard. |
  | Set a deadline on the step | From a date field (due 17:00 that day) or in N hours. |
  | Post to Teams / Slack / Google Chat | Sends a message through the channel's incoming-webhook URL. |
  | Add a comment | Writes an automatic note on the document. |
  | Notify people in the app | Bell notification to people and/or a group, without email. |

  The **Automation** page (workflow › Bots) is now a compact list: on/off switches, run and failure counts for the last 30 days, last result, search, and filters for on, off, conditional and failing bots.
- **Branding**:
  - **Email templates**: a designed HTML email with a header, logo, colours, footer and a button for the link; sender name and reply-to address; per-email on/off, CC, BCC and button label; click-to-insert placeholders; a live preview; "Send me a test".
  - **Theme**: 6 presets; colours for buttons, accent, page, header and header text; 10 fonts (loaded on recipient pages); sharp, rounded or pill corners; outlined, filled or underline fields; logo and name switches; a live preview.
  - **Thank you page**: icon or logo; placeholders (`{document_name}`, `{reference}`, `{field:…}`); reference number; a "what happens next" list; a contact line; button; auto-redirect after N seconds; confetti; a live preview.
- **Inbox, Documents and workflow pages** use a compact one-line row per document: status icon, name, workflow, version and ID, up to 2 tags plus a count, current step and holder, progress (done/total), deadline, last activity, and a ⋯ menu with every action, including Edit tags.

## Super admin console (`/platform/`)

Django superusers get a shield icon in the left rail that opens the console. Every action in it is recorded in the **Admin audit log**.

| Page | What it does |
|---|---|
| Overview | Platform counts, documents started per day, the busiest workspaces, and health checks (scheduler, bot and webhook failures, email delivery, public address, debug mode, secret key, AI models, super admins without two-step verification, maintenance mode). |
| Workspaces | Search, filter (suspended, plan expired), CSV export, create a workspace for an existing user or by invitation. Each workspace page covers members (role, remove, add or invite), plan and expiry, credits, admin-only notes, per-workspace feature switches, joining as a member, suspend/restore, and permanent delete after typing the workspace ID. |
| Users | Search, filters (super admins, without two-step, never signed in, no workspace, deactivated) and CSV export. Create a user, who is emailed a set-password link. Per user: edit profile, add to a workspace, send a reset link, sign out everywhere, reset two-step verification, revoke API tokens, make or remove super admin, deactivate. You can't lock yourself out, and the last super admin can't be removed. |
| Broadcasts | In-app notification, optionally also emailed, to everyone, one workspace and/or certain roles. Keeps a history. |
| Platform settings | Site name, support email, sign-up mode (open / allowed domains / invitation only / closed), defaults for new workspaces, workspaces per user, minimum password length, required two-step verification (admins or everyone), idle sign-out, upload size cap and blocked file types, blocking private networks for connectors/webhooks, a site-wide announcement banner (with an end time) and **maintenance mode** (everyone but super admins sees a "back soon" page; the API returns 503). Also on/off switches for AI, connectors, webhooks, REST API/MCP, public start forms and workspace creation. |
| Background jobs | Scheduler heartbeat and "Run jobs now"; delayed bots (run now or cancel); webhook deliveries waiting or failed (retry); recent bot failures. |
| Sign-in log | Every sign-in, failed attempt (including wrong two-step codes) and sign-out, with IP and device. Accounts with 3 or more failures in 24 h are flagged. |
| System & health | Versions, database and upload sizes, configuration, Django's `check --deploy` results, clear cache. |

## Custom connectors and AI in workflows

**Connectors** (*My workspace › Connectors*) wrap any HTTP/JSON API, such as a policy system, a CRM or an n8n/Zapier webhook.
A connector has a base URL, an auth method (none, bearer, header key, basic or query key), an optional secret and extra headers, and a list of **actions**:
- method and path;
- query parameters;
- an optional JSON body template;
- a response mapping, one `Field name = json.path` per line.

In paths, query values and bodies, `{field:Name}` inserts a document or form value and `{value}` inserts what was typed into a form lookup.
Use connectors with:
- the **Run a custom connector** bot, which writes the mapped answer into fields (a failure is logged and never stops the workflow);
- start-form lookups (source *A custom connector*);
- the MCP tools `list_connectors` and `run_connector`.

Each action has a **Test** button that sends the request and shows the response and the mapped values.

**AI & models** (*My workspace › AI & models*) picks the model for AI features: a local **Ollama** (auto-discovered, and each workspace can set its own address), an OpenAI-compatible server or Claude.
Only field values are sent, never signatures or files. The model powers:
- the bots **AI: fill fields**, **AI: summarize the document** and **AI: choose an option** (answers are checked against field types and options);
- the **✦ Ask AI** tab on every document, with quick prompts and an option to save the answer as a comment;
- the MCP tool `ask_document`;
- *Describe it* in the workflow builder.

The fill page shows **“N required fields left · Next ↓”**, which jumps to the next empty required field. Multi-document steps are long, so this saves scrolling.

## Security and API access

- **Two-factor sign-in** (My account › Security): authenticator-app codes (RFC 6238, QR rendered locally with
  `segno`), 10 single-use recovery codes (stored hashed), replay protection, 6 wrong codes → start over.
- **Where you're signed in**: device, IP and time per session; sign out one or all others.
- **API tokens** (My account › API & AI access): full or read-only, expiry, revocable, stored as SHA-256, 120 requests
  per minute. Use them with:
  - **Remote MCP** (Streamable HTTP): `POST /mcp` —
    `claude mcp add --transport http paperlane http://127.0.0.1:8000/mcp --header "Authorization: Bearer <token>"`.
    Read-only tokens only see the read tools; requests from other web origins are refused.
  - **REST API** `/api/v1/` (GET it for the endpoint list): workflows (list, get blueprint, build, publish, start,
    trash), documents (list, get, complete the open step), forms (list, get, build, publish, submit), team,
    spreadsheets, and `POST /api/v1/tools/<tool>` for any MCP tool.
- Workflows can be exported as a **blueprint** (workflow page › ⋯ › Export blueprint) and imported again (Import
  accepts Paperlane exports and blueprints).

## Sign-in and public pages

- `/` shows the landing page to visitors (also at `/welcome/`) and the dashboard to signed-in members.
- Log in has "Keep me signed in" (unticked = session ends with the browser), show/hide password, safe `next`
  redirects and throttling (8 failures per email or IP → 15-minute wait).
- Password reset by email: `/accounts/password-reset/` (unknown addresses get the same response, no email).
- Log out is POST-only (a plain visit shows a confirmation), so links or prefetching can't sign people out.

## Importing workflows

* **UI:** Workflows › *Import* accepts a Paperlane export (`.paperlane.json`, from a workflow's *More actions › Export
  definition*) or a workflow blueprint (`.blueprint.json`). An import report lists documents, steps, groups, bots and notes.
* **CLI:** `python manage.py import_workflow my-workflow.paperlane.json --workspace demo --user owner --publish`

## Project layout

```
paperlane/      settings, root URLs, WSGI
accounts/      workspaces, memberships and roles, groups, invitations, profiles (2FA, out of office), contacts,
               connected accounts, email templates, usage, API tokens; sign-in, onboarding, team and workspace settings
flows/         the product core
  engine.py        routing: start, advance, complete, conditions context, audit, notifications
  bots.py          bot catalog, handlers (h_<type>), executor, scheduler tick
  conditions.py    rule evaluation and formulas        fieldtypes.py  field types and validation
  sla.py           deadlines, escalation, out-of-office delegation
  duplicates.py    duplicate check                      mentions.py    @mentions in comments
  mailer.py        templated, branded HTML email        webhooks.py    signed outgoing webhooks with retries
  connectors.py    custom HTTP/JSON connectors          intake.py      public start forms and in-form lookups
  ai_providers.py  Ollama / OpenAI-compatible / Claude  ai_actions.py  AI fill, summarize, classify, Ask AI
  ai_builder.py    "Describe it" planner                blueprint.py   build, export and import workflows as JSON
  mcp.py           MCP server (stdio and HTTP)          exports.py     PDF / CSV / ZIP
  checks.py        builder checks (lint before publish) undo.py        builder undo / redo stacks
  views/           library, builder, editor, fill, packets, forms, workload, integrations, webhooks, api, ai
  management/commands/  seed_demo, seed_stories, seed_bank_demo, paperlane_tick, paperlane_mcp, import_workflow, build_claims_workflow
core/          dashboard and analytics, inbox, search, notifications, archive and trash, the `ui` template tags,
               HTML sanitizer, shared queries and helpers; test_deep.py (end-to-end and route-sweep tests)
superadmin/    platform console (/platform/), platform settings and feature switches, sign-in log, broadcasts,
               middleware (maintenance, suspension, idle sign-out, required 2FA), network guard
templates/     Django templates (about 150)
static/        css/app.css, js/ (app, editor, fill, forms, analytics)
docs/          ARCHITECTURE.md, DEPLOYMENT.md
```
