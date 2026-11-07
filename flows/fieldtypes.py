"""Registry of form elements and widgets available in the Paperlane document editor."""

ELEMENTS = [
    ("Basic", [
        ("heading", "Heading", "H"),
        ("text", "Single line text", "T"),
        ("textarea", "Multi line text", "¶"),
        ("description", "Description", "≡"),
        ("email", "Email", "@"),
        ("date", "Date", "▦"),
        ("signature", "Signature", "✍"),
        ("initials", "Initials", "IN"),
        ("number", "Number", "7"),
        ("phone", "Phone", "☎"),
        ("currency", "Currency", "$"),
        ("formula", "Formula", "fx"),
    ]),
    ("Choice", [
        ("checkbox", "Checkbox", "☑"),
        ("radio", "Radio button", "◉"),
        ("dropdown", "Dropdown", "▾"),
    ]),
    ("Grouped", [
        ("matrix", "Matrix", "⊞"),
        ("table", "Table", "▤"),
        ("group", "Group", "▣"),
        ("repeatable", "Repeatable container", "⧉"),
    ]),
    ("Other", [
        ("divider", "Divider", "—"),
        ("page_break", "Page break", "⤓"),
    ]),
]

WIDGETS = [
    ("Basic widgets", [
        ("attachment", "Attachment request", "📎"),
        ("address", "Address", "📍"),
        ("image", "Image", "🖼"),
        ("image_choice", "Image choice", "🖼"),
        ("rating", "Rating", "★"),
        ("youtube", "YouTube", "▶"),
        ("vimeo", "Vimeo", "▶"),
        ("contact_details", "Contact details", "☏"),
        ("personal_info", "Personal information", "🪪"),
        ("full_name", "Full name", "👤"),
        ("clickwrap", "Clickwrap", "✔"),
        ("html", "HTML code", "</>"),
    ]),
]

DISPLAY_ONLY = {"heading", "description", "divider", "page_break", "image", "youtube", "vimeo", "html"}

COMPOSITES = {
    "address": [("street", "Street address"), ("street2", "Address line 2"), ("city", "City"),
                ("state", "State / Region"), ("zip", "ZIP / Postal code"), ("country", "Country")],
    "contact_details": [("email", "Email"), ("phone", "Phone")],
    "personal_info": [("first_name", "First name"), ("last_name", "Last name"),
                      ("date_of_birth", "Date of birth"), ("gender", "Gender")],
    "full_name": [("first_name", "First name"), ("middle_name", "Middle name"), ("last_name", "Last name")],
}

LABELS = {t: label for _, items in ELEMENTS + WIDGETS for t, label, _ in items}

CONDITION_OPERATORS = [
    ("eq", "is"), ("neq", "is not"), ("contains", "contains"), ("not_contains", "does not contain"),
    ("empty", "is empty"), ("not_empty", "is not empty"), ("gt", "greater than"), ("lt", "less than"),
    ("gte", "greater or equal"), ("lte", "less or equal"), ("checked", "is checked"), ("not_checked", "is not checked"),
    ("regex", "matches regex"), ("after", "is after"), ("until", "is on or before"),
]

# Condition types other than a document field (values supplied by engine.condition_context).
CONDITION_TYPES = [
    ("When the recipient", [("@user.email", "Email"), ("@user.first_name", "First name"), ("@user.last_name", "Last name"),
                            ("@user.position", "Position"), ("@user.role", "Access level")]),
    ("When the date", [("@date", "Date (YYYY-MM-DD)"), ("@time", "Time (HH:MM)"), ("@datetime", "Date and time"),
                       ("@day", "Day of the week"), ("@month", "Month")]),
    ("When the previous step", [("@prev.status", "Status (completed / declined)")]),
    ("When document data", [("@revision", "Revision number"), ("@tag", "Tag")]),
]


def condition_groups(documents):
    """[(group label, [(key, label)])] for condition pickers: the fixed types plus per-document counters."""
    docs = list(documents)
    return CONDITION_TYPES + [
        ("When the number of filled fields in", [(f"@filled:{d.key}", d.name) for d in docs]),
        ("When all fields of a document", [(f"@all_fillable:{d.key}", f"{d.name} · all fillable fields") for d in docs]
         + [(f"@all_required:{d.key}", f"{d.name} · all required fields") for d in docs]),
    ]


def condition_label(key, documents=()):
    """Readable label for a special condition key, or None when it is not one the picker offers."""
    for group, items in condition_groups(documents):
        for k, label in items:
            if k == key:
                return f"{group.replace('When the ', '').replace('When ', '').capitalize()}: {label}"
    return None

SPECIAL_CONDITION_FIELDS = [
    ("@step", "Acting step (key)"), ("@status", "Step result (FILLED / DECLINED)"), ("@document", "Document (key)"),
]


def field_roles(field):
    """A field's role assignment may be "anyone", one step key or a list of step keys."""
    role = field.get("role") or "anyone"
    return role if isinstance(role, list) else [role]


DATE_FORMATS = [("MM/dd/yyyy", "%m/%d/%Y"), ("dd/MM/yyyy", "%d/%m/%Y"), ("yyyy-MM-dd", "%Y-%m-%d"), ("dd MMM yyyy", "%d %b %Y")]


def format_date(value, fmt):
    from datetime import date

    try:
        d = date.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value)
    return d.strftime(dict(DATE_FORMATS).get(fmt, "%Y-%m-%d"))


def is_input(field_type):
    return field_type not in DISPLAY_ONLY


def default_field(field_type, index=1):
    """Defaults used when a new field is dropped on the canvas (mirrors static/js/editor.js)."""
    base = {"type": field_type, "label": LABELS.get(field_type, field_type.title()), "required": False}
    if field_type in ("radio", "dropdown", "checkbox"):
        base["options"] = ["Option 1", "Option 2"]
    if field_type == "matrix":
        base.update(rows=["Row 1", "Row 2"], columns=["Column 1", "Column 2"])
    if field_type == "table":
        base.update(columns=["Column 1", "Column 2"])
    if field_type in ("group", "repeatable"):
        base["subfields"] = [{"id": "sub1", "type": "text", "label": "Field"}]
    return base


def field_value_to_text(field, value):
    """Human readable rendering of any stored value (used for exports, emails, certificates)."""
    if value in (None, "", [], {}):
        return ""
    t = field.get("type")
    if t == "date" and field.get("date_format"):
        return format_date(value, field["date_format"])
    if t == "attachment":
        items = value if isinstance(value, list) else [value]
        return ", ".join(i.get("name", "") for i in items if isinstance(i, dict))
    if t in ("signature", "initials"):
        return "[signed]" if value else ""
    if t == "clickwrap":
        return "Agreed" if value else "Not agreed"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        if value and isinstance(value[0], dict):
            return "; ".join(", ".join(f"{k}: {v}" for k, v in row.items() if v not in ("", None)) for row in value)
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        if t == "attachment":
            return value.get("name", "")
        if t == "matrix":
            return "; ".join(f"{k}: {v}" for k, v in value.items())
        return ", ".join(str(v) for v in value.values() if v not in ("", None))
    return str(value)
