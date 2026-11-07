"""Help center articles. Bodies are trusted HTML written here (never user input). ``<span class="hp">A › B</span>``
marks a menu path. Keep articles short, task-first, and in step with the product."""
import re

CATEGORIES = [
    {"slug": "getting-started", "title": "Getting started", "icon": "play", "desc": "The basics: workspaces, the main screens and your first workflow."},
    {"slug": "workflows", "title": "Workflows & the builder", "icon": "workflow", "desc": "Steps, recipients, branches, jumps, conditions, deadlines and publishing."},
    {"slug": "documents", "title": "Documents & tracking", "icon": "documents", "desc": "Follow, revise, download and manage the documents your workflows create."},
    {"slug": "forms", "title": "Forms & the editor", "icon": "edit", "desc": "Build fillable documents and public start forms with lookups and formulas."},
    {"slug": "recipients", "title": "Filling & signing", "icon": "send", "desc": "What recipients see, signing, drafts, declining and group work queues."},
    {"slug": "automation", "title": "Bots & automation", "icon": "bot", "desc": "Bots, autostart, reminders, reference numbers, duplicates and chaining."},
    {"slug": "ai", "title": "AI features", "icon": "sparkle", "desc": "AI assistant, Describe it, Ask AI, AI bots, fill from text and AI search."},
    {"slug": "team", "title": "Team & permissions", "icon": "team", "desc": "Roles, groups, workload, out of office and access to workflows."},
    {"slug": "integrations", "title": "Integrations & API", "icon": "link", "desc": "Connectors, webhooks, REST API, MCP and importing workflows."},
    {"slug": "workspace", "title": "Workspace & security", "icon": "shield", "desc": "Branding, email templates, two-step sign-in, sessions and audit."},
]

A = []


def art(cat, slug, title, summary, body, keywords="", popular=False):
    A.append({"cat": cat, "slug": slug, "title": title, "summary": summary, "body": body.strip(), "keywords": keywords,
              "popular": popular})


# ---- Getting started -----------------------------------------------------------------------------------------------
art("getting-started", "tour", "A quick tour of Paperlane",
    "Where everything lives: the rail, the sidebar, Create, Inbox, Documents and Workflows.",
    """
<p>Paperlane automates document processes: a <b>workflow</b> sends one or more documents through steps — people fill,
review, approve and sign — while <b>bots</b> do the routine work in between.</p>
<h3>The screen</h3>
<ul>
<li><b>Icon rail</b> (far left): search <kbd>⌘</kbd><kbd>K</kbd>, team, contacts, help, notifications and your profile.</li>
<li><b>Sidebar</b>: the workspace switcher, <b>Create</b>, Dashboard, Inbox, Documents, Templates, Workflows, Forms, Team workload, recent workflows, Archive and Trash.</li>
<li><b>Dashboard</b>: ready-made automations to start from, and Analytics with charts you can configure.</li>
</ul>
<h3>The words we use</h3>
<ul>
<li><b>Workflow</b> — the process definition (steps, documents, bots). You edit it in the <b>builder</b> and <b>Publish</b> it.</li>
<li><b>Document</b> — one run of a workflow, e.g. “Claim POL-100003”. Track it on <span class="hp">Documents</span>.</li>
<li><b>Template</b> — a reusable single-recipient document.</li>
<li><b>Step</b> — one person (or group) filling or reviewing. <b>Branch</b> — a split with rules.</li>
</ul>
""", "overview intro start basics navigation screen layout", popular=True)

art("getting-started", "first-workflow", "Build your first workflow",
    "From a blank workflow (or a plain-English description) to a published process in minutes.",
    """
<ol>
<li>Click <span class="hp">Create › Workflow</span>. Or describe the process in words with <span class="hp">Workflows › Describe it</span> and let AI draft it.</li>
<li>In the <b>builder</b>, add a <b>document</b> in the Documents panel (bottom left): <b>New form</b> to build fields, or upload a PDF/Word file.</li>
<li>Click <b>+</b> on the diagram to add steps. Open a step's ⚙ to choose the recipient (email, group, contact, the current user or anyone with the link) and what they can do with each document.</li>
<li>Open the document in the editor and give fields a <b>role</b> — the step that fills them.</li>
<li>Click <b>Test</b> to run it yourself, then <b>Publish</b>. Only published versions can be run.</li>
</ol>
<p>Next: share it with a link, a start form, a CSV or a schedule — see <a href="/help/workflows/start-and-share/">Start and share a workflow</a>.</p>
""", "create new workflow first steps publish test builder", popular=True)

art("getting-started", "inbox", "Your Inbox: what's waiting for you",
    "Find the steps assigned to you or your groups, and pick up group work.",
    """
<p><span class="hp">Inbox</span> lists documents where you have something to do, plus ones you took part in. The list, and the same kind of list on Documents, Workflows and a document's own page, refreshes on its own when someone sends, completes or takes a step. The Inbox count in the sidebar updates with it.</p>
<ul>
<li><b>Waiting for me</b> — steps assigned to you, to a group you belong to, or to someone you're covering for (out of office).</li>
<li><b>Completed</b> — documents you finished.</li>
<li>Switch between <b>Cards</b> and <b>Compact</b> layouts with the icons above the list; your choice is remembered.</li>
</ul>
<p>Group steps show <b>Take this task</b> — see <a href="/help/recipients/group-queue/">Group work queues</a>.</p>
""", "inbox waiting for me tasks assigned to me queue")

art("getting-started", "search-shortcuts", "Search and keyboard shortcuts",
    "Jump anywhere with ⌘K and find documents fast.",
    """
<ul>
<li><kbd>⌘</kbd>/<kbd>Ctrl</kbd> + <kbd>K</kbd> — search workflows, templates, documents and contacts from any page. <kbd>↑</kbd><kbd>↓</kbd> to move, <kbd>↵</kbd> to open, <kbd>esc</kbd> to close.</li>
<li>On <span class="hp">Documents</span>, type a name, workflow, tag or recipient email in the search box — or describe what you want and press <b>✦</b> (see <a href="/help/ai/ai-search/">AI search</a>).</li>
<li>In the builder: <kbd>⌘</kbd>/<kbd>Ctrl</kbd> + <kbd>↵</kbd> sends a request to the AI assistant.</li>
</ul>
""", "keyboard shortcut cmd k ctrl k search find palette")

# ---- Workflows -----------------------------------------------------------------------------------------------------
art("workflows", "recipients", "Choose who does each step",
    "Recipients: an email, a field value, a contact, a group, the current user or anyone with the link.",
    """
<p>Open a step's ⚙ in the builder. Recipient types:</p>
<ul>
<li><b>Email</b> — a fixed address, or a field from earlier in the document: <code>{field:Manager email}</code>.</li>
<li><b>Group</b> — everyone in a team group; one member takes it (see <a href="/help/recipients/group-queue/">work queues</a>).</li>
<li><b>Contact</b>, <b>Current user</b> (whoever runs it), or <b>Anyone with the link</b> (public first steps).</li>
</ul>
<p>The same drawer sets the invitation subject and message (with a preview), per-document access (<b>fill / view / hidden</b>), attachments, reminders, access expiration, deadlines, the thank-you text and whether the recipient may decline, discard or save a draft.</p>
""", "recipient assignee who step email group contact anyone link assign", popular=True)

art("workflows", "branches", "Branches, conditions and jumps",
    "Send documents down different paths based on what was filled in.",
    """
<p><b>Branches</b> split the diagram into named lanes. Each lane has rules; the first lane whose rules match is taken, otherwise the workflow continues after the branches. A lane can hold its own steps, complete the workflow, or go to any step (loops allowed).</p>
<p><b>Conditions</b> can be put on steps, lanes, documents, fields and bots. Rules test:</p>
<ul>
<li>a field (is, is not, contains, regex, empty, greater/less than, dates before/after) — or <code>@all:</code>/<code>@any:</code> several fields at once;</li>
<li>the recipient, a date or weekday, the status of an earlier step, the revision number, a tag, or how many fields are filled;</li>
<li><code>@duplicate</code> — whether a duplicate check matched.</li>
</ul>
<p><b>Jumps</b> (step ⚙ › Jumps) move to another step when their rules match — handy for “send back for more information”.</p>
""", "branch condition rule if lane jump loop route routing decision approve reject", popular=True)

art("workflows", "builder-tools", "Checks, undo and finding steps in the builder",
    "Spot broken links before you publish, undo any change, and jump to a step in a large workflow.",
    """
<h3>Checks</h3>
<p>The button next to <b>✦ AI assistant</b> shows what the builder found: <b>Must fix</b> (red) stops Publish, <b>Warnings</b> (amber)
are things that will make documents behave unexpectedly, and tips are advice. Steps with a problem get a red or amber mark on the
diagram. The checks look for:</p>
<ul>
<li>jumps, branches and bots that point at a step that was deleted;</li>
<li>conditions, branch rules and bot rules that read a field that no longer exists (names must match exactly);</li>
<li><code>{field:…}</code> tokens in bot emails and settings that match no field;</li>
<li>a branch with no rules placed before others — it is always taken, so the ones after it never run;</li>
<li>recipient steps with no email, groups with no members, deadlines that reassign to no one;</li>
<li>required fields assigned to a deleted step, and bot steps with no bot.</li>
</ul>
<p>Each item has <b>Open</b> (the step's settings), <b>Show on canvas</b>, and where it helps <b>✦ Ask AI to fix</b>.</p>
<h3>Undo and redo</h3>
<p>Every change in the builder — adding, moving, renaming or deleting steps, step settings, documents, bots, autostart and AI
changes — can be undone with <b>↶</b> or <kbd>⌘</kbd><kbd>Z</kbd> (<kbd>Ctrl</kbd><kbd>Z</kbd>) and redone with <b>↷</b> or
<kbd>⇧</kbd><kbd>⌘</kbd><kbd>Z</kbd>. The last 30 changes are kept. Making a new change after undoing clears redo.</p>
<h3>Publishing</h3>
<p><b>Publish</b> first shows what changes since the live version, any warnings, and how many documents are in progress
(they finish on the version they started with). Add a note for the audit trail, then publish.</p>
<p>Changed your mind? <b>Discard changes</b> in the same window puts the editor back to the live version (undo with ↶).
Restoring an older version from the workflow page can be undone the same way.</p>
<h3>Insights</h3>
<p>Each workflow's <b>Insights</b> shows time per step, <b>where documents stop</b> (how many reached each step and how many are
waiting, declined or cancelled there), and a <b>week-by-week</b> table of volume, deadline success and the slowest step.
<b>Export CSV</b> downloads all of it.</p>
<h3>Live activity</h3>
<p>Each step shows how many documents are <b>waiting</b> there (click to see them), how many are <b>overdue</b>, and how
long the step <b>usually</b> takes once it has a few completions.</p>
<h3>Approvals in one click</h3>
<p>In the <b>+</b> menu choose <b>✅ Approval</b>: you get an approver step, a required <i>Approve / Reject</i> field and
comments for it, and a branch that completes the workflow on Reject. Then choose who approves.</p>
<h3>Step templates</h3>
<p>On a step choose <b>⋯ › 📌 Save as template</b>. Its settings, the fields it fills and its bots are saved for the whole
workspace; add it to any workflow from the <b>+</b> menu under <i>Your step templates</i>. If a field name is already used,
the copy is renamed (“Amount (2)”) and its rules follow. Manage them in <span class="hp">⋯ › Step templates</span>.</p>
<h3>Replace a recipient</h3>
<p>When someone leaves, <span class="hp">⋯ › Replace a recipient…</span> swaps their email for a new one in every step,
notification list, deadline escalation and bot email of the workflow. Documents already running keep the old address.</p>
<h3>Fix all with AI</h3>
<p>When Checks lists several fixable problems, <b>✦ Fix all with AI</b> sends them as one request to the AI assistant;
review the proposal and apply it.</p>
<h3>Big diagrams</h3>
<p>Click <b>▾</b> on a branch step to collapse its branches; finding a step inside opens them again.</p>
<h3>Moving around</h3>
<ul>
<li><b>Find a step</b> (press <kbd>/</kbd>): type part of a name, then <kbd>Enter</kbd> to jump to it. Each result shows who does the step.</li>
<li><kbd>F</kbd> fits the whole workflow on screen; <b>1:1</b> returns to actual size; <kbd>⌘</kbd>/<kbd>Ctrl</kbd> + scroll zooms.</li>
<li><span class="hp">⋯ › Export blueprint (JSON)</span> downloads a readable copy you can import into another workspace or give to an AI assistant.</li>
</ul>
""", "checks problems warnings errors validate lint broken undo redo history find search step zoom fit export blueprint shortcuts", popular=True)

art("workflows", "approvals", "Add an approval with Approve / Reject",
    "An approver step, its decision field and a branch that ends the workflow on Reject.",
    """
<ol>
<li>In the builder click <b>+</b> where the approval should happen and add a <b>step</b>, e.g. “Manager approval”. Set its recipient (an email, a group or a field such as <code>{field:Manager email}</code>).</li>
<li>Open the document in the editor and add a <b>Radio</b> field “Manager approval decision” with the options <b>Approve</b> and <b>Reject</b>. Make it <b>required</b> and give it the approver's <b>role</b> so only that step fills it. Add a “Comments” field if you want a reason.</li>
<li>Back in the builder, click <b>+</b> right after the approval step and add a <b>Branch</b>.</li>
<li>Open the branch ⚙, add a lane “Rejected” with the rule <b>Manager approval decision is Reject</b>, and set what happens: <b>Complete the workflow</b> (or go to a step that informs the requester).</li>
<li>Approved documents don't match the lane, so they continue to the next step after the branch. <b>Test</b>, then <b>Publish</b>.</li>
</ol>
<p>Faster: in the builder open <b>✦ AI assistant</b> and ask “Add a Manager approval after the request with Approve/Reject; rejected ends the workflow” — or describe the whole process in <span class="hp">Workflows › Describe it</span>.</p>
<p>Want rejected documents to go back for changes instead? Point the lane to the earlier step, or use a <b>jump</b> (see <a href="/help/workflows/branches/">Branches, conditions and jumps</a>).</p>
""", "approval approve reject decline approver decision branch manager review sign off", popular=True)

art("workflows", "deadlines", "Deadlines and escalation",
    "Give steps a due time and escalate or reassign when they run late.",
    """
<p>Builder › step ⚙ › <b>Advanced settings › Deadline</b>: complete within N hours, days or business days.</p>
<ul>
<li>Cards show <b>⏱ Due in …</b>; documents get <b>Due</b> / <b>Overdue</b> pills; <span class="hp">Documents › Overdue</span> lists late ones.</li>
<li>When a deadline passes the holder and the workflow owner are told, an optional tag is added, and the step is escalated to a person/group or <b>reassigned</b> (with a fresh deadline).</li>
<li>The <b>Set a deadline on the step</b> bot sets a due time from a date field.</li>
</ul>
""", "deadline sla overdue due escalate escalation late timer")

art("workflows", "publish-versions", "Publish, versions and testing",
    "Why changes need Publish, how to test safely and how to restore an old version.",
    """
<ul>
<li>Edits in the builder are a <b>draft</b> until you click <b>Publish</b>. Running documents keep the version they started with.</li>
<li><b>Test</b> starts a test document only you see (marked <i>Test</i>) so you can walk every step.</li>
<li>Publishing checks the workflow first and lists anything that would break it.</li>
<li>Workflow page › <b>Version history</b> lets you restore an older version into the editor; publish to make it live again.</li>
<li>Only one person edits at a time — others see “The process is in use” and can request access or take over.</li>
</ul>
""", "publish version draft test unpublished changes restore history lock in use")

art("workflows", "start-and-share", "Start and share a workflow",
    "Run it yourself, share a public link, send in bulk, start from a form or on a schedule.",
    """
<ul>
<li><b>Run</b> — workflow page › Run, and fill the first step.</li>
<li><b>Link</b> — Distribute › Via link: anyone with the link starts a new document.</li>
<li><b>Bulk</b> — upload a CSV: first column <code>email</code>, other columns named after fields.</li>
<li><b>Start form</b> — a public form that starts the workflow (see <a href="/help/forms/start-forms/">Start forms</a>).</li>
<li><b>Autostart</b> — builder › Start › Add autostart: on a schedule, from an Excel/CSV file, when another workflow completes, or from an external system via its webhook.</li>
</ul>
""", "share link run start distribute bulk csv autostart schedule public")

# ---- Documents -----------------------------------------------------------------------------------------------------
art("documents", "track", "Track a document",
    "See where a document is, who holds it and what happened.",
    """
<p>Open any document from <span class="hp">Documents</span> or the Inbox. Tabs:</p>
<ul>
<li><b>Status</b> — the step diagram, who holds the current step, and an ✦ <b>AI summary</b>.</li>
<li><b>Path</b> — the workflow diagram with this document's route: green steps are done (who did them and how long it took), the blue one is where it waits now, grey ones were skipped.</li>
<li><b>Documents</b> (data), <b>Revisions</b>, <b>Comments</b> (type @ to mention), <b>✦ Ask AI</b>, <b>Files</b>, <b>Audit trail</b>, <b>Bots log</b> and <b>Emails</b>.</li>
</ul>
<p>From the current step you can <b>remind</b>, <b>resend</b>, <b>reassign</b> or <b>copy the link</b>.</p>
""", "status where is my document track progress who has it current step path route diagram", popular=True)

art("documents", "filters", "Find documents: filters, tags and quick views",
    "Quick views, filters, sort, tags and bulk actions on the Documents page.",
    """
<ul>
<li>Quick views: <b>Needs attention</b>, <b>No activity for 7+ days</b>, <b>Overdue</b>, <b>My documents</b>.</li>
<li>Filter by status, workflow, tag and dates; sort by activity, creation date or name.</li>
<li>Tick documents (or the box in the header) for bulk <b>Download</b> (ZIP of PDFs), <b>Export metadata</b>, <b>Add tags</b>, <b>Lock</b>, <b>Archive</b> or <b>Delete</b>.</li>
<li>Tags are added by bots or with <b>+ Add tags</b>; click a tag to see every document with it.</li>
</ul>
""", "filter tag sort bulk download export find overdue stale")

art("documents", "revise", "Revise, send back or cancel a document",
    "Correct a completed step, send work back, or stop a document.",
    """
<ul>
<li><b>Revise</b> (⋯ › Revise) sends the document back to an earlier step; the next completion creates a new revision. Every revision can be viewed and downloaded.</li>
<li><b>Lock</b> stops further changes; <b>Cancel</b> stops a document that is in progress.</li>
<li><b>Archive</b> hides it from lists; <b>Delete</b> moves it to Trash (restorable).</li>
</ul>
""", "revise send back correction mistake undo cancel stop revision")

art("documents", "download", "Download, print and send PDFs",
    "PDFs with a certificate of completion and audit trail, printable views and emailing a copy.",
    """
<ul>
<li><b>Download</b> — a PDF of every document, with a certificate of completion and the audit trail.</li>
<li><b>Printable view</b> for quick printing; <b>Export metadata</b> or the audit trail as CSV.</li>
<li><b>Save and send</b> emails a PDF copy to anyone.</li>
</ul>
""", "pdf download print export certificate audit email copy send")

# ---- Forms ---------------------------------------------------------------------------------------------------------
art("forms", "editor", "The document editor",
    "Fields, widgets, layout, roles, properties, formulas, conditions and design.",
    """
<ul>
<li>Add <b>elements</b> (text, number, date, dropdown, checkbox, signature, attachment…) and <b>widgets</b> (tables, repeatable groups, matrix, rating, image choice, video) by clicking or dragging.</li>
<li><b>Roles</b> decide which step fills each field (colour-coded). Adding a new role adds a step.</li>
<li><b>Properties</b>: label, variable name, placeholder, help, required, default, options, min/max, length, regex, half width, hidden or read-only for recipients.</li>
<li><b>Formulas</b> calculate live from other fields. <b>Conditions</b> show a field only when rules match.</li>
<li><b>Design</b>: width, font, logo and colours. Undo/redo and Preview are in the toolbar.</li>
</ul>
""", "editor field fields form element widget formula calculation role required design layout page", popular=True)

art("forms", "start-forms", "Start forms with lookups",
    "Public forms that start a workflow and pre-fill details from a spreadsheet, earlier documents or an API.",
    """
<ol>
<li><span class="hp">Forms › New form</span> — start blank or from a template (claim intake, beneficiary change, change of address, surrender, service request, employee details).</li>
<li>Pick the workflow it starts. Values map to workflow fields by name (or an explicit mapping); files are attached.</li>
<li>Add <b>in-form bots</b>: when a trigger field (e.g. Policy number) is filled, look it up in a Paperlane spreadsheet, earlier documents or a <a href="/help/integrations/connectors/">connector</a> and fill other fields.</li>
<li>Publish, then share the link or embed it on your website.</li>
</ol>
<p>The form can count as the first step, or send the first step to the email typed in the form.</p>
""", "form start form public web form intake lookup prefill embed website wordpress")

# ---- Recipients ----------------------------------------------------------------------------------------------------
art("recipients", "fill-sign", "Filling and signing a document",
    "What recipients see, signatures, drafts and the required-fields helper.",
    """
<ul>
<li>Recipients open the emailed link — no account needed for external people.</li>
<li>Each document is its own page. <b>Next document</b> opens the following one.</li>
<li><b>“N required fields left · Next ↓”</b> jumps to the next empty required field, including one on a later document.</li>
<li>Signatures and initials can be drawn, typed or reused from the profile.</li>
<li><b>Save as draft</b> keeps progress; <b>Decline</b> asks for a reason and notifies the sender.</li>
<li><b>✦ Fill from text</b> — paste an email or letter and AI fills the fields (see <a href="/help/ai/fill-from-text/">Fill from text</a>).</li>
</ul>
""", "fill sign signature recipient draft decline required fields external", popular=True)

art("recipients", "group-queue", "Group work queues",
    "How a group step is picked up, released or taken over.",
    """
<ul>
<li>On a group step a member clicks <b>Take this task</b>. While they hold it, the rest of the group no longer sees it.</li>
<li>Saving a draft or completing takes it automatically. The holder can <b>Release</b> it back to the group.</li>
<li>Supervisors and owners can <b>Take over</b>; <span class="hp">Team workload</span> shows every queue.</li>
</ul>
""", "group queue claim take task release take over team")

# ---- Automation ----------------------------------------------------------------------------------------------------
art("automation", "bots", "Bots: automate the routine work",
    "Triggers, conditions and the most useful bots.",
    """
<p>Open a workflow's <b>Bots</b> (builder top bar › All bots) and pick one from the catalog. Every bot has a <b>trigger</b> (workflow start, step sent, opened, completed, workflow complete) and optional <b>conditions</b>.</p>
<ul>
<li><b>Data</b>: pre-fill (values, another document, a spreadsheet, CSV or API), transform or combine fields, generate a reference number.</li>
<li><b>Routing</b>: assign to the next team member, set a deadline, start another workflow.</li>
<li><b>Messages</b>: custom email, in-app notification, comment, Teams/Slack/Google Chat.</li>
<li><b>Records</b>: rename, tags, lock, archive, export to a spreadsheet, webhook, connectors and ~40 “Save to …” services.</li>
<li><b>AI</b>: fill fields, summarize, choose an option, review against rules, translate (see <a href="/help/ai/ai-bots/">AI bots</a>).</li>
</ul>
<p>The Bots page shows on/off switches, run and failure counts and the last result. Each document has a <b>Bots log</b>.</p>
""", "bot bots automation trigger prefill rename tag email webhook automate", popular=True)

art("automation", "autostart", "Autostart: schedules, files and other workflows",
    "Start documents automatically.",
    """
<p>Builder › <b>Start</b> › <b>Add autostart</b>:</p>
<ul>
<li><b>Schedule</b> — once, every N minutes/hours/days/weeks, or on chosen weekdays/month days; start date, timezone and stop rule.</li>
<li><b>From a file</b> — an Excel or CSV file: one document per row, or the next row on each run.</li>
<li><b>From another workflow</b> — when it completes, with field pre-fill.</li>
<li><b>From an external event</b> — POST to the workflow's webhook URL.</li>
</ul>
""", "autostart schedule recurring cron excel csv file trigger external webhook")

art("automation", "duplicates-references", "Duplicate checks and reference numbers",
    "Catch repeat submissions and number documents.",
    """
<ul>
<li><b>Duplicate check</b> (workflow page › ⋯ › Duplicate check): compare up to 5 fields with documents in progress or from the last N days. Matches get a tag, a comment linking the other document, a warning banner and a notification.</li>
<li><b>Generate a reference number</b> bot: patterns like <code>WD-{yyyy}-{seq:5}</code>, optional yearly restart, can be added to the document name.</li>
</ul>
""", "duplicate reference number sequence numbering repeat")

art("automation", "reminders-email", "Reminders and notification emails",
    "Remind recipients and control which emails go out.",
    """
<ul>
<li>Step ⚙ › <b>Reminders</b>: repeat every N days until the step is done; <b>Access expiration</b> closes the link.</li>
<li>Email wording and design: <span class="hp">My workspace › Email templates</span> (per event, with CC/BCC and a live preview).</li>
<li>Every sent email is in the document's <b>Emails</b> tab and the workspace <b>Email log</b>.</li>
</ul>
""", "reminder remind email notification expire expiration template")

# ---- AI ------------------------------------------------------------------------------------------------------------
art("ai", "assistant", "AI assistant in the builder",
    "Ask AI to review or change the workflow you're editing — and compare before and after.",
    """
<ol>
<li>In the builder click <b>✦ AI assistant</b>. A panel opens next to the diagram.</li>
<li><b>Review my workflow</b> lists suggestions (missing approvals, fields nobody fills, notifications); problems found by <b>Checks</b> come first. <b>Propose this</b> asks for the change straight away; <b>Edit request</b> copies it into the box first. The suggested requests under the box name your own steps and fields.</li>
<li>Describe a change — “Add a Finance approval after Legal when the amount is over 10,000” — and click <b>Propose</b>.</li>
<li>The canvas switches to <b>After ✦</b>: new steps are outlined green, changed ones orange. Toggle <b>Before</b> to compare. The panel lists every change.</li>
<li><b>Apply to workflow</b> saves it as unpublished changes. Undo it with <b>↶</b> in the top bar (<kbd>⌘</kbd><kbd>Z</kbd>). Nothing is published until you click Publish.</li>
</ol>
<p><b>Refine proposal</b> adjusts the proposal you're looking at ("make it 5,000 instead"); <b>New request</b> starts
again from the saved workflow. <b>Explain</b> writes a plain-English summary (who does what, decisions, automatic steps,
deadlines) you can copy and share.</p>
<p>The model's name is shown at the top of the panel; while it works you see what it's doing and how long it has taken, and <b>Cancel</b> stops waiting.</p>
<p>Tips: name the steps and fields you mean; one change per request works best; larger models handle bigger restructures.</p>
""", "ai assistant builder improve change edit workflow suggest before after compare undo", popular=True)

art("ai", "agent-bot", "AI agent: automate a step in plain English",
    "Write what should happen; the AI does only the actions you allow.",
    """
<ol>
<li>Open <span class="hp">Bots › Add bot › AI agent: follow instructions</span> and choose when it runs (for example when a step is completed).</li>
<li>Write the instructions as you would to a colleague: “If the claim mentions a previous claim in the last 12 months, set Risk to High, tag it #repeat and alert the owner. Otherwise set Risk to Low.”</li>
<li>Choose what it may do: the <b>fields it may set</b> (by name), <b>add tags</b>, <b>write a comment</b>, <b>alert the document's owner</b>. Anything else the model suggests is ignored.</li>
</ol>
<p>Values are checked against each field (a choice field only accepts its options, a date must be a date). Sensitive fields are never sent to the AI or changed. The Bots log shows what it did and why.</p>
<h3>Write invitation emails with AI</h3>
<p>In a step's settings, under the email message, pick a tone (friendly, formal, short) and click <b>✦ Write with AI</b>. Edit the result, then Apply.</p>
""", "ai agent bot instructions plain english automate agent studio tags alert comment email write tone draft")

art("documents", "key-dates", "Key dates and renewal reminders",
    "Track contract ends, renewals and expiries from finished documents, with reminders before each one.",
    """
<ol>
<li>In the builder open <span class="hp">⋯ › Key dates &amp; reminders</span>. Date fields with names like “expiry” or “renewal” are marked <i>Suggested</i>.</li>
<li>Tick the dates to track, name them, choose how many days before to remind (for example <code>30, 7, 0</code>) and who else to tell.</li>
<li>When a document finishes, its dates appear on <span class="hp">Documents › Key dates</span>. Whoever started the document (and the people you listed) get an email and a notification before each date.</li>
</ol>
<p>Turning it on also picks up documents that already finished. Mark a date <b>Handled</b> to stop its reminders.</p>
""", "key dates renewal expiry contract end reminder obligation deadline review anniversary")

art("recipients", "client-portal", "Client portal: one page for all a recipient's documents",
    "Recipients see everything waiting for them and download what they finished.",
    """
<p>An admin turns it on in <span class="hp">My workspace › Workspace settings › Client portal</span>. Share the address
<code>/portal/&lt;your workspace&gt;/</code> (for example on your website or in email signatures).</p>
<ol>
<li>The recipient enters the email address their documents were sent to.</li>
<li>They get an email with a sign-in link that works for 24 hours.</li>
<li>The page lists what is <b>waiting for them</b> (open it to fill and sign) and what they <b>finished</b>, with a PDF download of completed documents. Sensitive fields are masked in the PDF.</li>
</ol>
<p>The page answers the same way whether or not an address has documents, so it can't be used to find out who works with you.</p>
""", "client portal recipient portal customer portal magic link my documents download external")

art("ai", "ask-documents", "Ask your documents, the watchdog and the weekly note",
    "Find documents by meaning with cited answers; get alerts about unusual documents; read a weekly summary.",
    """
<h3>✦ Ask your documents</h3>
<p>On <span class="hp">Documents › ✦ Ask your documents</span> type a question — “claims over 10,000 still waiting for a decision”.
Paperlane finds the matching documents you are allowed to see and the AI answers with references like <b>#123</b> that open them.
With an embedding model (AI &amp; models › Search model; for Ollama run <code>ollama pull nomic-embed-text</code>) it matches
by meaning, so “new hire paperwork” finds onboarding forms; without one it matches words. Sensitive fields are never searched.</p>
<h3>Watchdog</h3>
<p>Turn on <span class="hp">AI &amp; models › Watchdog</span>. After each step Paperlane compares the document with the rest of its
workflow and flags an amount far outside the usual range, or the same person, email or phone in several recent documents. The
owner gets a notification and the document shows the finding; <b>Looks fine</b> dismisses it. The checks are statistical, so
they never invent problems.</p>
<h3>✦ This week</h3>
<p><span class="hp">Team workload</span> opens with a short plain-English note on the week — volume, slow steps, who has the
most open work, key dates coming up — and the same note leads the weekly team report email.</p>
""", "ask documents semantic search meaning embedding citations watchdog anomaly unusual amount duplicate weekly digest summary")

art("automation", "word-templates", "Generate documents from Word templates",
    "Fill a .docx you designed in Word with the document's data, and save it (and a PDF) to the document.",
    """
<ol>
<li>Write the template in Word. Type placeholders as plain text: <code>{Policy number}</code>, <code>{document_name}</code>,
<code>{date}</code>. Keep text only when a rule holds with <code>{if Amount &gt; 10000}</code> … <code>{endif}</code> (also
<code>=</code>, <code>!=</code>, <code>&lt;</code>, <code>contains</code>; a lone <code>{if Field}</code> means “has a value”). Blocks can span
paragraphs and table rows.</li>
<li>In the builder's Documents panel, <b>Upload</b> the .docx.</li>
<li>Add the bot <span class="hp">Documents › Generate a document from a Word template</span>, enter the template's name and
a file name such as <code>{document_name} – offer letter</code>, and usually run it when the workflow completes.</li>
</ol>
<p>The filled file appears in the document's <b>Files</b> tab. Tick <b>Also save a PDF</b> if LibreOffice is installed on the
server. Placeholders that match no field are left empty and listed in the Bots log.</p>
""", "word docx template generate document merge fields placeholders offer letter contract pdf libreoffice")

art("recipients", "verify-recipient", "Verify a recipient before they open documents",
    "Ask for a code by email, text message, or a code only they know.",
    """
<p>In a step's settings open <b>Advanced settings › Verify the recipient</b> and choose:</p>
<ul>
<li><b>Code by email</b> — a 6-digit code sent to the recipient's address.</li>
<li><b>Code by text message</b> — sent to a phone number (often <code>{field:Phone}</code>) through an SMS gateway connection.</li>
<li><b>Access code</b> — a code you share separately, or a field they should know, such as <code>{field:Policy number}</code>.</li>
</ul>
<p>Codes last 10 minutes; five wrong answers lock the link for 15 minutes. Every attempt is in the audit trail.</p>
<h3>One-tap decisions from the email</h3>
<p>When choosing <i>Approve</i> / <i>Reject</i> (or similar) is all a step needs, its invitation email includes a link for each
choice. The link opens a short confirmation page — so email security scanners can't decide anything by opening it — and the
recipient can add a comment. Turn it off per step under <b>Signing process › One-tap decision in the email</b>.</p>
""", "verify identity code otp sms access code security recipient one tap approve reject email decision")

art("integrations", "cloud-drives", "Save documents to Google Drive, OneDrive or SharePoint",
    "File finished documents in cloud folders named from their fields.",
    """
<ol>
<li>Your administrator adds the Google or Microsoft app keys to the server once (see the deployment guide).</li>
<li>Under <span class="hp">Account › Connections</span> click <b>Connect Google Drive</b> or <b>Connect Microsoft 365</b> and sign in.</li>
<li>Add the bot <span class="hp">Documents › Save to cloud drive</span>: choose the connection and a folder such as
<code>Claims/{field:Claim type}/{document_name}</code>. Folders are created as needed. For a SharePoint library enter its drive id.</li>
</ol>
<p>Tick <b>Also upload files generated by bots</b> to send Word-template output along with the PDF.</p>
""", "google drive onedrive sharepoint microsoft 365 cloud storage save upload folder oauth connect")

art("ai", "describe-it", "Describe it: build a workflow from words",
    "Write the process in plain English and get a complete draft.",
    """
<p><span class="hp">Workflows › Describe it</span> (also in Create). Write one sentence per step — “Employee submits item, amount and reason. Manager approves. If amount is over 5,000, Finance approves. Email the requester when done.” Pick an engine, review the preview (steps, recipients, conditions, fields, bots) and click <b>Create as draft</b> or <b>Create and publish</b>.</p>
<p>The built-in planner works offline and instantly; a language model (Ollama, OpenAI-compatible or Claude) follows richer descriptions.</p>
""", "describe it ai generate create workflow from text plain english prompt")

art("ai", "ask-ai", "Ask AI and AI summaries on documents",
    "Ask questions about a document and get a short summary for the next person.",
    """
<ul>
<li>Document › <b>✦ Ask AI</b>: ask anything about its values — follow-up questions keep the conversation. Tick “Save answers as comments” to share them.</li>
<li>Document › Status › <b>✦ AI summary</b>: a short summary that refreshes when the data changes.</li>
<li>Only field values are sent to the model — never signatures or uploaded files.</li>
</ul>
""", "ask ai question document summary summarize chat")

art("ai", "ai-bots", "AI bots",
    "Fill fields, summarize, classify, review against rules and translate — automatically.",
    """
<ul>
<li><b>AI: fill fields</b> — derive values (e.g. risk level, next action) from the document. Answers are checked against field types and options.</li>
<li><b>AI: summarize the document</b> — into a field and/or a comment.</li>
<li><b>AI: choose an option</b> — pick a dropdown/radio value such as priority.</li>
<li><b>AI: review against rules</b> — write rules in plain English; the bot writes Passed / Needs review, tags problem documents and lists issues in a comment.</li>
<li><b>AI: translate fields</b> — <code>Description = Description (Spanish)</code>, one pair per line.</li>
</ul>
<p>Bots never invent values: when the data doesn't support an answer the field is left empty and the Bots log says so.</p>
""", "ai bot fill classify summarize review rules translate translation")

art("ai", "fill-from-text", "Fill from text (recipients)",
    "Recipients paste an email, letter or notes and AI fills the form for them.",
    """
<p>On the fill page recipients click <b>✦ Fill from text</b>, paste text and click <b>Fill fields</b>. Only the fields they're allowed to edit on that step are filled; nothing is saved until they complete the step. Filled fields flash so they can be checked.</p>
<p>Turn it on or off in <span class="hp">My workspace › AI &amp; models</span>.</p>
""", "fill from text paste email extract recipient autofill")

art("ai", "ai-search", "AI search on Documents",
    "Describe what you're looking for instead of setting filters.",
    """
<p>Type something like “declined claims this month” or “overdue documents for the onboarding workflow” in the Documents search box and press <b>✦</b>. The request becomes real filters (status, workflow, tag, dates, quick views) shown above the results. If no model is available it falls back to a normal text search.</p>
""", "ai search natural language find documents filter")

art("ai", "models", "Choose and manage the AI model",
    "Ollama, Anthropic, NVIDIA, OpenAI, or a custom endpoint — and the usage log.",
    """
<ul>
<li><span class="hp">My workspace › AI &amp; models</span>: choose one provider — Ollama, Anthropic, NVIDIA, OpenAI, or a custom address — and one model. Every AI feature uses it. An optional backup is used only when that provider cannot be reached.</li>
<li>A local <b>Ollama</b> model keeps every document on your own machines. A cloud key sends field values to that provider. Keys already saved stay when you switch, and are not asked for again.</li>
<li>The <b>Usage</b> section shows calls, average time and failures for the last 7 days. Open a row to see who ran it, the model, the document, and — when a call failed — the reason and the model reply.</li>
</ul>
""", "model ollama claude anthropic nvidia openai llm settings usage local privacy engine")

# ---- Team ----------------------------------------------------------------------------------------------------------
art("team", "roles", "Roles and permissions",
    "What owners, supervisors, auditors, creators, fillers and guests can do.",
    """
<ul>
<li><b>Workspace Owner</b> — full control of the workspace, billing and teammates.</li>
<li><b>Supervisor</b> — manages teammates, workflows and every document; sees Team workload.</li>
<li><b>Auditor</b> — can view every workflow and document, read-only, plus the audit trail.</li>
<li><b>Creator</b> — creates workflows and templates and sends documents.</li>
<li><b>Filler</b> — fills out documents and runs shared workflows.</li>
<li><b>Guest</b> — only completes documents sent to them.</li>
</ul>
<p>Workflow access is <b>private</b> or <b>team</b>, and can be shared with people or groups (workflow page › Manage access).</p>
""", "role roles permission access owner supervisor auditor admin member creator filler guest", popular=True)

art("team", "invite-groups", "Invite teammates and create groups",
    "Add people, change roles and organise groups.",
    """
<ul>
<li>Rail › <b>Team</b> › <b>Invite</b>: email and role. Pending invitations can be resent or revoked.</li>
<li>Groups (Team › Groups) are used as step recipients and work queues. Add members one by one or in bulk.</li>
<li>Contacts (rail › Contacts) store external people; import or export CSV.</li>
</ul>
""", "invite teammate user add member group groups contacts")

art("team", "workload-ooo", "Team workload and out of office",
    "See who holds what, rebalance work and cover for absences.",
    """
<ul>
<li><span class="hp">Team workload</span>: open and overdue steps per person and per group queue, oldest item and recent throughput. Managers can bulk-reassign or release.</li>
<li><span class="hp">My account › Out of office</span>: dates plus a teammate who covers. New steps go to them and your group work appears in their Inbox.</li>
</ul>
""", "workload out of office vacation delegate cover reassign balance")

# ---- Integrations --------------------------------------------------------------------------------------------------
art("integrations", "connectors", "Custom connectors (any HTTP/JSON API)",
    "Connect your policy system, CRM or an n8n/Zapier hook.",
    """
<p><span class="hp">My workspace › Connectors</span>: a base URL, an auth method (bearer, header key, basic, query key), and <b>actions</b> — method, path, query, optional JSON body and a response mapping (<code>Field name = json.path</code>). Use <code>{field:Name}</code> and <code>{value}</code> in paths and bodies. Each action has a <b>Test</b> button.</p>
<p>Use connectors in the <b>Run a custom connector</b> bot, in start-form lookups, and from MCP.</p>
""", "connector api http json integration crm lookup external system")

art("integrations", "webhooks", "Webhooks",
    "Send signed events to your systems when documents change.",
    """
<p><span class="hp">My workspace › Webhooks</span>: pick events (document started/completed, step sent/completed/declined/overdue, form submitted, comment added…), an optional workflow filter and fields. Requests are signed with <code>X-Paperlane-Signature: sha256=…</code>; failures retry for up to 12 hours. The delivery log has Send test and Redeliver.</p>
""", "webhook events signature hmac notify external")

art("integrations", "api-mcp", "REST API and MCP (Claude and other AI tools)",
    "Tokens, the REST API and connecting Claude via MCP.",
    """
<ul>
<li><span class="hp">My account › API &amp; AI access</span>: create a full or read-only token.</li>
<li>REST: <code>/api/v1/</code> lists endpoints for workflows, documents, forms, team and spreadsheets.</li>
<li>MCP: <code>claude mcp add --transport http paperlane &lt;site&gt;/mcp --header "Authorization: Bearer &lt;token&gt;"</code> lets Claude build workflows, start them and complete steps.</li>
</ul>
""", "api rest token mcp claude integration developer automation key")

art("integrations", "import", "Import and export workflows",
    "Move workflows between workspaces or bring them in from a configuration export.",
    """
<p>Workflow page › ⋯ › <b>Export blueprint</b> saves a workflow as JSON. <span class="hp">Workflows › Import</span> accepts Paperlane exports and blueprints, and shows an import report.</p>
""", "import export blueprint json migrate move copy")

# ---- Workspace -----------------------------------------------------------------------------------------------------
art("workspace", "branding", "Branding: theme, emails and thank-you page",
    "Make recipient pages and emails look like your company.",
    """
<ul>
<li><b>Theme</b> — presets, colours, fonts, corners and field style, with a live preview.</li>
<li><b>Email templates</b> — header, logo, colours, footer and button; sender name and reply-to; send yourself a test.</li>
<li><b>Thank-you page</b> — icon or logo, placeholders, reference number, “what happens next”, redirect and confetti.</li>
</ul>
""", "brand branding logo colors theme email template thank you design")

art("workspace", "security", "Two-step sign-in, sessions and audit",
    "Protect your account and see what happened in the workspace.",
    """
<ul>
<li><span class="hp">My account › Security</span>: authenticator-app codes with recovery codes, and every place you're signed in (sign out one or all).</li>
<li>Rail › <b>Audit trail</b> (owners, supervisors, auditors): a day-by-day timeline of everything in the workspace, with CSV export.</li>
<li>Forgot your password? Use <b>Forgot password</b> on the sign-in page.</li>
</ul>
""", "security 2fa two factor mfa password session audit log sign in login")

art("workspace", "what-went-wrong", "“What went wrong?” — diagnose a document and report problems",
    "Let the assistant find why a document failed or got stuck, and file a support ticket when it's serious.",
    """
<ol>
<li>Open the document. If something is wrong a banner shows the most serious problem — click <b>✦ What went wrong?</b>. You can also open the assistant (✦ in the rail) on any document and ask in your own words, or type <code>#123</code> to ask about document 123 from anywhere. Failed rows in a Bots log have a <b>✦ What went wrong?</b> link too.</li>
<li>The assistant checks the document: failed bots (and how many other documents the same bot failed on), steps nobody can open, expired links, missing or invalid recipients, invitations that weren't sent, overdue steps, failed webhooks and declines.</li>
<li>You get a plain-language explanation, each problem with its <b>fix</b>, and a severity: <b>Low</b>, <b>Medium</b>, <b>High</b> or <b>Critical</b>. Severity rises with impact — a problem on 3+ documents is at least High, on 10+ Critical.</li>
<li><b>High and Critical</b> problems file a <b>support ticket automatically</b> (one per problem — it's updated when more documents hit it). For anything else click <b>Create a support ticket</b>.</li>
</ol>
<p>Follow tickets in <span class="hp">Support tickets</span> (sidebar): add notes, change the status, or click <b>Check the document again</b> after fixing it. Owners and supervisors are notified of new tickets; platform support replies appear in the ticket's activity.</p>
""", "what went wrong debug diagnose failed error stuck broken ticket support report problem bug incident urgent", popular=True)

art("workspace", "troubleshooting", "Troubleshooting common problems",
    "Recipients not getting emails, steps not moving, bots failing.",
    """
<ul>
<li><b>Recipient didn't get the email</b> — check the document's <b>Emails</b> tab, then <b>Resend</b> or <b>Copy link</b> on the current step.</li>
<li><b>Changes don't show</b> — the workflow has unpublished changes; click <b>Publish</b>. Running documents keep their version.</li>
<li><b>A step was skipped</b> — check its conditions and earlier branch rules; the Status tab shows which path was taken.</li>
<li><b>A bot failed</b> — open the document's <b>Bots log</b>; the Bots page shows failures for the last 30 days.</li>
<li><b>“The process is in use”</b> — someone else is editing; request access or take over.</li>
<li><b>AI features missing</b> — an owner can check <span class="hp">My workspace › AI &amp; models</span>.</li>
</ul>
""", "problem not working error email not received skipped bot failed troubleshoot help issue", popular=True)


# ---- 1.5 ---------------------------------------------------------------------------------------------------------
art("documents", "verify", "Sealed documents and the verify page",
    "Every completed document is sealed so anyone can check it wasn't changed.",
    """
<p>When a document completes, Paperlane computes a <b>SHA-256 fingerprint</b> of every field value and signature, the
contents of every attached file and the signing timeline. It prints the fingerprint and a <b>verify code</b> on the
certificate of completion and stores the final PDF unchanged — <b>Download</b> always gives that exact file.</p>
<ul>
<li>Anyone can check a document at <code>/verify/</code>: enter the code (e.g. <code>ABCD-1234-EF56-7890</code>) or upload the PDF.</li>
<li>On the document's <b>Status</b> tab, <b>Check integrity</b> re-hashes the stored record and warns if anything changed after sealing.</li>
<li>If a document is revised and completed again it gets a new seal; the old code shows “a newer version exists”.</li>
</ul>
""", "seal sealed verify tamper fingerprint hash certificate authentic pdf integrity", popular=True)

art("workspace", "retention", "Data retention and legal hold",
    "Delete finished documents, emptied trash and old audit events automatically.",
    """
<ol>
<li>Owners open <span class="hp">My workspace › Data retention</span>.</li>
<li>Set periods in days (0 = keep forever): finished documents (choose completed, declined and/or cancelled), Trash, and the audit trail.</li>
<li>The preview shows how many items the rules would delete today. Rules run once a day; attached files and sealed PDFs are removed too, and each run is recorded in the audit trail.</li>
</ol>
<p><b>Legal hold</b>: open a document's ⋯ menu › <b>Legal hold</b>. Held documents are never deleted by any rule.</p>
""", "retention delete purge legal hold gdpr keep years trash audit")

art("workspace", "sso", "Single sign-on (Microsoft, Google, Okta)",
    "Let your team sign in with their company account.",
    """
<ol>
<li>A platform admin opens <span class="hp">Super admin › Single sign-on</span> and copies the <b>redirect URI</b>.</li>
<li>Register a web app in your identity provider with that URI, then add the issuer, client ID and secret.</li>
<li>List the <b>allowed email domains</b> (required) — only those people can sign in. Optionally create accounts on first sign-in and add them to a workspace with a default role.</li>
<li>A “Sign in with …” button appears on the login page. Use <b>Test discovery</b> to check the setup.</li>
</ol>
""", "sso single sign-on microsoft entra azure ad google workspace okta oidc openid login")

art("team", "digests", "Email digests and team reports",
    "A daily or weekly summary email instead of checking the app.",
    """
<ul>
<li><span class="hp">My account › Notifications</span> › <b>My digest</b>: what's waiting for you, what's overdue or due today, and documents you sent that completed or were declined.</li>
<li>Owners, supervisors and auditors can also get a <b>Team report</b>: started, completed and overdue work by person and queue, plus open high and critical tickets.</li>
<li>Choose Daily or Weekly (Mondays). They're sent after 7:00 and only when there's something to report. Use <b>Send me a preview</b> to see one now.</li>
</ul>
""", "digest summary email daily weekly report team report overdue notifications")

art("forms", "payments", "Take payments on a start form",
    "Charge a fee before the workflow starts, with Stripe Checkout.",
    """
<ol>
<li>Open the form's <b>Settings › Payment</b>, tick <b>Require a payment</b> and set a fixed amount — or take it from a number field — and the currency.</li>
<li>When someone submits, their answers are saved and they're sent to a secure Stripe page. Card details never reach Paperlane.</li>
<li>The workflow starts as soon as the payment is confirmed, and the payment is recorded in the document's audit trail. Unpaid submissions show “Awaiting payment”.</li>
</ol>
<p>An administrator sets <code>PAPERLANE_STRIPE_SECRET_KEY</code> and <code>PAPERLANE_STRIPE_WEBHOOK_SECRET</code> on the server first.</p>
""", "payment pay fee stripe charge card checkout money form")

art("workflows", "compare-versions", "Compare workflow versions",
    "See exactly what changed between two published versions, or since the last publish.",
    """
<p>Workflow page › <b>Version history</b> › <b>Compare versions</b> (or <b>Compare</b> next to a version). Pick two versions — or a version and the current draft — to see added, changed and removed steps, documents, fields and bots, with the properties that changed (recipient, condition, required, options…).</p>
""", "compare versions diff changes history what changed publish")

art("ai", "ask-across", "Ask questions about all your documents",
    "“Which claims over 10,000 are waiting on Legal?” — answered from your real data.",
    """
<p>Open the assistant (✦ in the rail) and ask about your documents: <i>how many…</i>, <i>which…</i>, <i>list…</i>, <i>total/average…</i>.
The AI only turns your question into a query; Paperlane runs it with <b>your</b> permissions and calculates the numbers itself.
The answer shows how the question was interpreted, the count or total, and links to the matching documents.</p>
""", "ask ai all documents how many which list total report query search data")

art("recipients", "languages", "Recipient pages in Spanish",
    "Let people fill and sign in English or Español.",
    """
<ul>
<li>Recipients can switch between <b>English</b> and <b>Español</b> at the top of the fill page; their choice is remembered.</li>
<li>Set the default in <span class="hp">My workspace › Workspace settings › Recipients</span>: English, Español, or match the recipient's browser.</li>
<li>Your own form content (labels, emails, thank-you text) is shown as you wrote it — write it in the language your recipients use.</li>
</ul>
""", "language spanish espanol español translate translation recipient")

# ---- lookups -------------------------------------------------------------------------------------------------------
# ---- 1.6 -----------------------------------------------------------------------------------------------------------
art("forms", "ai-builder", "Build a form with AI",
    "Describe a document, or upload the paper version, and the AI designs the form. Then edit or review it by instruction.",
    """
<p>Open a document in the editor and choose <span class="hp">AI builder</span> in the toolbar. The panel has three tabs.</p>
<ul>
<li><b>Build</b>: describe the document (for example “Death claim with bank details for direct deposit”), or open
<b>Start from an existing form</b> to upload a PDF, Word file or photo of a paper form. You see a preview first, then choose
<b>Use this form</b> or <b>Add to the end</b>.</li>
<li><b>Edit</b>: ask for a change in plain words, such as “make all contact fields required” or “add a date of birth after Name”.</li>
<li><b>Review</b>: get suggestions and apply them one at a time or all at once.</li>
</ul>
<p>Every change goes through Undo (<kbd>⌘</kbd><kbd>Z</kbd>), and nothing is saved until you save the document. The AI marks
bank, tax, ID and medical fields as sensitive automatically.</p>
""", "ai form builder design import pdf word photo paper form edit review")

art("forms", "sensitive-fields", "Sensitive fields",
    "Mark bank, ID and medical fields as sensitive: they are never sent to AI and are masked for people not involved.",
    """
<p>Select a field in the editor and tick <span class="hp">Properties › Sensitive</span>.</p>
<ul>
<li>Its value is <b>never sent to an AI model</b>: not to AI bots, Ask AI, summaries, search or the AI builder.</li>
<li>People who aren't involved see it masked (<code>•••• 6789</code>) on the document page, in CSV exports and in PDFs.
The creator, anyone who filled the document and managers see the full value.</li>
<li>Field history stores sensitive values masked too.</li>
</ul>
""", "sensitive privacy mask bank account id medical pii")

art("ai", "test-bench", "Compare AI models with the test bench",
    "Score the active model, or its backup, on six real Paperlane tasks.",
    """
<p>In <span class="hp">My workspace › AI &amp; models</span>, pick a model under <b>Model test bench</b> and choose
<b>Run benchmark</b>. The menu lists the active provider’s models and, when you set one, the backup’s. It runs six tasks
with checkable answers (designing and editing a form, search filters, classifying, summarising and editing a workflow)
and records the score and average time.</p>
""", "benchmark test bench score model speed compare models")

art("documents", "saved-views", "Saved views",
    "Save a set of Documents filters with a name, see its live count, and share it with your team.",
    """
<p>Set the filters you want on <span class="hp">Documents</span> (status, workflow, tags, dates, search), then choose
<b>Save this view</b>. Name it and, if you like, tick <b>Share with my team</b>.</p>
<p>Saved views appear as chips above the list with a live count. Click one to apply it. You can remove your own views,
and managers can remove shared ones.</p>
""", "saved views filters chips share")

art("documents", "field-history", "Field history: who changed what",
    "Every change to a document's values, with who made it and whether it came from a person, a step or a bot.",
    """
<p>Open a document and choose the <span class="hp">History</span> tab. Each row shows the field, the old and new value,
who changed it and how: a step (<i>Step: Manager approval</i>), a bot (<i>Bot: Rename</i>), an email or an edit. Use the
field filter to follow one value through the whole process.</p>
<p>Changes are recorded from the moment this feature was installed. Sensitive values are stored masked.</p>
""", "history audit changes who changed value old new")

art("forms", "field-library", "Reuse sections with the field library",
    "Drop ready-made sections into any form, or save your own section once and reuse it everywhere.",
    """
<p>In the editor, open <span class="hp">Library</span> in the left toolbar.</p>
<ul>
<li><b>Starter sections</b>: bank details (shown only for direct deposit), contact details, declaration and signature,
beneficiaries and address with proof.</li>
<li><b>Your workspace</b>: select a heading to save the whole section under it (or a single field), name it and choose
<b>Save to library</b>. It's then available in every form in the workspace.</li>
</ul>
<p>Inserted fields get new IDs, keep their show/hide rules, and are filled by the role you're editing.</p>
""", "library reuse section bank details declaration template fields")

art("recipients", "quick-approvals", "Quick approvals on your phone",
    "Steps that only need approve or reject appear in Approvals as one-tap cards.",
    """
<p><span class="hp">Approvals</span> in the sidebar lists the steps waiting for you that only need a decision
(Approve / Reject, Accept / Decline and similar), with the key facts and the AI summary. Add a comment if the step has one,
then tap the decision. Reject asks you to confirm.</p>
<p>A step that needs more than a decision (other required fields, a signature) shows <b>Open to complete</b> instead.
Sensitive values show as <code>••••</code> on the card.</p>
""", "approvals mobile phone one tap approve reject")

art("workflows", "simulate", "Simulate a workflow before you publish",
    "Run test documents through every branch, jump and loop. Nothing is kept, and no emails or outside bots are sent.",
    """
<p>In the builder, choose <span class="hp">Simulate</span> next to Test. Paperlane fills the draft with sample answers,
then varies the answers your conditions check, so each lane, jump and bot route gets a test run. Each run is undone
afterwards.</p>
<p>The report shows how many steps were reached and every distinct route, with the answers that lead to it, who each
step goes to and which emails and bots would run. It flags:</p>
<ul>
<li>steps no answers lead to, and conditions that check a field that doesn't exist;</li>
<li>steps that go to a group with no members;</li>
<li>jumps back to a step that won't run again because <b>Recurrence</b> is off, and steps skipped the second time round;</li>
<li>routes that loop or stop, and required fields nobody fills.</li>
</ul>
<p>Large workflows are explored until every step is reached or the run limit is hit; the report says when it stopped early.</p>
""", "simulate simulator test branches coverage unreachable dry run")

art("automation", "email-start", "Start a workflow from an email",
    "Forward an email to the workflow's address: a document starts and the AI fills the first step from the message.",
    """
<p>In the builder, choose <span class="hp">Add autostart › From an email</span>. The workflow gets its own address.
Forward or send a message to it and Paperlane:</p>
<ul>
<li>starts a document named after the subject;</li>
<li>fills the first step's fields from the email with AI (sensitive fields are left for people);</li>
<li>saves attachments and keeps the email as a comment;</li>
<li>sends the sender a link to check the answers and submit.</li>
</ul>
<p>By default only members of the workspace can start it; you can also allow senders from listed domains. The same
message never starts two documents.</p>
<p><b>Setup (administrators):</b> set <code>PAPERLANE_INBOUND_DOMAIN</code> and <code>PAPERLANE_INBOUND_SECRET</code>, then
point your mail provider's inbound webhook (Mailgun, SendGrid or Postmark) at <code>/inbound/email/&lt;secret&gt;/</code>,
or pipe messages from your own mail server to <code>python manage.py inbound_email</code>.</p>
""", "email forward inbound start mailgun sendgrid postmark autostart")


CAT_BY_SLUG = {c["slug"]: c for c in CATEGORIES}
for a in A:
    a["url"] = f"/help/{a['cat']}/{a['slug']}/"
    a["cat_title"] = CAT_BY_SLUG[a["cat"]]["title"]
    a["text"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", a["body"])).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()
for c in CATEGORIES:
    c["articles"] = [a for a in A if a["cat"] == c["slug"]]

STOP = set("a an the and or of to in on for with is are be can i my me how do does what where why when it this that you your".split())


def _tokens(text):
    return [t for t in re.findall(r"[a-z0-9@]+", text.lower()) if t not in STOP and len(t) > 1]


def search(query, limit=6):
    """Simple ranked keyword search over titles, keywords, summaries and bodies."""
    q = _tokens(query)
    if not q:
        return []
    scored = []
    for a in A:
        title, kw, summ, body = (_tokens(a["title"]), _tokens(a["keywords"]), _tokens(a["summary"]), _tokens(a["text"]))
        score = 0.0
        for t in q:
            stem = t[:5]
            score += 4 * sum(w.startswith(stem) for w in title) + 3 * sum(w.startswith(stem) for w in kw)
            score += 1.5 * sum(w.startswith(stem) for w in summ) + 0.4 * min(5, sum(w.startswith(stem) for w in body))
        if score:
            scored.append((score, a))
    scored.sort(key=lambda x: -x[0])
    return [a for _, a in scored[:limit]]


def get(cat, slug):
    return next((a for a in A if a["cat"] == cat and a["slug"] == slug), None)
