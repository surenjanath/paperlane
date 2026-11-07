"""Condition evaluation shared by steps, branches, documents, bots and form fields."""
import ast
import operator
import re
from decimal import Decimal, InvalidOperation


def _num(v):
    try:
        return Decimal(str(v).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _as_text(v):
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    if isinstance(v, dict):
        return ", ".join(str(x) for x in v.values())
    return "" if v is None else str(v)


_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december"]


def _when(v):
    """Parse a date / time / date-time / weekday / month into a comparable tuple (kind, value), else None."""
    import datetime as _dt

    t = _as_text(v).strip()
    if not t:
        return None
    low = t.lower()
    for names, kind in ((_DAYS, "day"), (_MONTHS, "month")):
        for i, n in enumerate(names):
            if low == n or low == n[:3]:
                return (kind, i)
    for fmt, kind in (("%Y-%m-%dT%H:%M:%S", "datetime"), ("%Y-%m-%dT%H:%M", "datetime"), ("%Y-%m-%d %H:%M", "datetime"),
                      ("%Y-%m-%d", "date"), ("%d/%m/%Y", "date"), ("%m/%d/%Y", "date"), ("%H:%M:%S", "time"), ("%H:%M", "time")):
        try:
            x = _dt.datetime.strptime(t[:19], fmt)
        except ValueError:
            continue
        return (kind, x.time() if kind == "time" else (x.date() if kind == "date" else x))
    return None


def _compare_when(actual, expected):
    """-1/0/1 comparing two temporal values of compatible kinds (a date-time is compared by date to a date)."""
    a, b = _when(actual), _when(expected)
    if not a or not b:
        return None
    (ka, va), (kb, vb) = a, b
    if ka != kb:
        if {ka, kb} == {"datetime", "date"}:
            va = va.date() if ka == "datetime" else va
            vb = vb.date() if kb == "datetime" else vb
        else:
            return None
    return (va > vb) - (va < vb)


def check_rule(rule, values):
    field = rule.get("field")
    if not field:
        return True
    if isinstance(field, str) and field.startswith("@any:"):  # true when any listed field matches
        names = [f.strip() for f in field[5:].split(",") if f.strip()]
        return any(check_rule({**rule, "field": n}, values) for n in names)
    if isinstance(field, str) and field.startswith("@all:"):  # every listed field must match
        names = [f.strip() for f in field[5:].split(",") if f.strip()]
        return bool(names) and all(check_rule({**rule, "field": n}, values) for n in names)
    op = rule.get("op", "eq")
    expected = rule.get("value", "")
    ref = re.fullmatch(r"\{field:(.+?)\}", str(expected).strip()) if isinstance(expected, str) else None
    if ref:  # compare with another field's value
        expected = values.get(ref.group(1).strip(), "")
    actual = values.get(field)
    if op == "checked":
        return actual not in (None, "", [], {}, False, "false", "off")
    if op == "not_checked":
        return actual in (None, "", [], {}, False, "false", "off")
    if str(field).startswith("@all_") and isinstance(actual, list):  # "All fillable / required fields"
        filled = [x not in (None, "", [], {}, False) for x in actual]
        if op == "empty":
            return not any(filled)
        if op == "not_empty":
            return bool(filled) and all(filled)
    if op == "empty":
        return actual in (None, "", [], {}, False)
    if op == "not_empty":
        return actual not in (None, "", [], {}, False)
    if op in ("after", "until"):
        c = _compare_when(actual, expected)
        return c is not None and (c > 0 if op == "after" else c <= 0)
    if op == "regex":
        try:
            return re.search(str(expected), _as_text(actual), re.IGNORECASE) is not None
        except re.error:
            return False
    if op in ("gt", "lt", "gte", "lte"):
        a, b = _num(actual), _num(expected)
        if a is None or b is None:
            return False
        return {"gt": a > b, "lt": a < b, "gte": a >= b, "lte": a <= b}[op]
    a_text = _as_text(actual).strip().lower()
    e_text = str(expected).strip().lower()
    if isinstance(actual, list):
        members = [str(x).strip().lower() for x in actual]
        if op == "eq":
            return e_text in members or a_text == e_text
        if op == "neq":
            return e_text not in members
    if op in ("eq", "neq") and a_text != e_text and _when(actual) and _when(expected):
        same = _compare_when(actual, expected) == 0  # 2026-10-01 equals 01/10/2026
        return same if op == "eq" else not same
    if op == "eq":
        return a_text == e_text
    if op == "neq":
        return a_text != e_text
    if op == "contains":
        return e_text in a_text
    if op == "not_contains":
        return e_text not in a_text
    return False


def _is_group(rule):
    return isinstance(rule, dict) and "rules" in rule


def _meaningful(rule):
    return _is_group(rule) and any(_meaningful(r) for r in rule["rules"]) or bool(isinstance(rule, dict) and rule.get("field"))


def evaluate(condition, values, context=None):
    """Evaluate a (possibly nested) condition.

    ``condition`` = {"logic": "and"|"or", "rules": [rule | group]} where a rule is {field, op, value}
    and a group is another condition dict. Empty conditions are true. ``context`` adds special
    values such as ``@step`` (acting step key), ``@status`` (FILLED / DECLINED), ``@document``.
    """
    if not condition:
        return True
    if context:
        values = {**values, **context}
    rules = [r for r in condition.get("rules", []) if _meaningful(r)]
    if not rules:
        return True
    results = [evaluate(r, values) if _is_group(r) else check_rule(r, values) for r in rules]
    return any(results) if condition.get("logic") == "or" else all(results)


def referenced_fields(condition):
    """All field references in a condition tree (used by importers and validation)."""
    out = []
    for r in (condition or {}).get("rules", []):
        if _is_group(r):
            out.extend(referenced_fields(r))
        elif r.get("field"):
            out.append(r["field"])
    return out


# ---- safe formula evaluation ---------------------------------------------------
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
}
_FUNCS = {"min": min, "max": max, "round": round, "abs": abs, "sum": lambda *a: sum(a)}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
        return _FUNCS[node.func.id](*[_eval(a) for a in node.args])
    raise ValueError("Unsupported expression")


def compute_formula(expression, values):
    """Formulas reference other fields as {Field name}. Returns a string result or ''."""
    if not expression:
        return ""

    def repl(m):
        n = _num(values.get(m.group(1).strip(), 0))
        return str(n if n is not None else 0)

    expr = re.sub(r"\{([^}]+)\}", repl, expression)
    try:
        result = _eval(ast.parse(expr, mode="eval"))
    except (ValueError, SyntaxError, ZeroDivisionError, TypeError, OverflowError):
        return ""
    if isinstance(result, float):
        result = round(result, 2)
        if result.is_integer():
            result = int(result)
    return str(result)
