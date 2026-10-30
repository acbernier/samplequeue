"""Condition engine.

A rule is a nested JSON tree the visual builder produces. Two node shapes:

    group: {"type": "group", "op": "AND" | "OR", "negate": false, "children": [...]}
    cond:  {"type": "cond", "field": "LCSET", "op": "not_empty"}

The same tree type is used for three things, which is why it is worth
keeping generic: which samples belong to a process (scope), which samples
land in a status, and which rows count as "open" at the source.

An empty group matches everything -- a status with no conditions is a
catch-all, which is what people reach for when they add a final
"Everything else" column.
"""
import re
from datetime import datetime, timezone

TRUE_STRINGS = {"t", "true", "y", "yes", "1"}
FALSE_STRINGS = {"f", "false", "n", "no", "0"}

# op -> (label, value arity). Arity: 0 none, 1 single, 2 range, "list" multi.
OPERATORS = {
    "text": {
        "eq": ("is", 1), "neq": ("is not", 1),
        "in": ("is any of", "list"), "not_in": ("is none of", "list"),
        "contains": ("contains", 1), "not_contains": ("does not contain", 1),
        "starts_with": ("starts with", 1), "ends_with": ("ends with", 1),
        "regex": ("matches regex", 1),
        "empty": ("is blank", 0), "not_empty": ("is not blank", 0),
    },
    "number": {
        "eq": ("=", 1), "neq": ("≠", 1),
        "gt": (">", 1), "gte": ("≥", 1), "lt": ("<", 1), "lte": ("≤", 1),
        "between": ("between", 2),
        "empty": ("is blank", 0), "not_empty": ("is not blank", 0),
    },
    "date": {
        "before": ("is before", 1), "after": ("is after", 1),
        "within_days": ("is within the last N days", 1),
        "older_than_days": ("is more than N days ago", 1),
        "next_days": ("is within the next N days", 1),
        "empty": ("is blank", 0), "not_empty": ("is not blank", 0),
    },
    "bool": {
        "is_true": ("is yes", 0), "is_false": ("is no", 0),
        "empty": ("is blank", 0), "not_empty": ("is not blank", 0),
    },
}

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
    "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M",
    "%m/%d/%Y", "%m/%d/%y",
    "%d-%b-%Y %I:%M:%S %p", "%d-%b-%Y", "%Y/%m/%d",
)

# Excel and Tableau exports put narrow/non-breaking spaces before AM/PM.
_ODD_SPACES = ("\u202f", "\u00a0", "\u2009", "\u200a")


def parse_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value).strip().replace("Z", "")
    for odd in _ODD_SPACES:
        text = text.replace(odd, " ")
    if "." in text:
        text = text.split(".", 1)[0]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_number(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("$", "").replace("%", "")
    try:
        return float(text)
    except ValueError:
        return None


def parse_bool(value):
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if text in TRUE_STRINGS:
        return True
    if text in FALSE_STRINGS:
        return False
    return None


def _is_blank(value):
    return value is None or (isinstance(value, str) and value.strip() == "")


def _as_list(value):
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    if _is_blank(value):
        return []
    return [str(value)]


def eval_cond(node, row, today=None):
    """Evaluate one leaf condition against a sample's attribute dict."""
    field = node.get("field")
    op = node.get("op", "eq")
    target = node.get("value")
    raw = row.get(field)

    if op == "empty":
        return _is_blank(raw)
    if op == "not_empty":
        return not _is_blank(raw)

    # Booleans
    if op in ("is_true", "is_false"):
        got = parse_bool(raw)
        return got is True if op == "is_true" else got is False

    # Dates
    if op in ("before", "after", "within_days", "older_than_days", "next_days"):
        got = parse_date(raw)
        if got is None:
            return False
        ref = today or datetime.now(timezone.utc).replace(tzinfo=None)
        if op in ("before", "after"):
            cut = parse_date(target)
            if cut is None:
                return False
            return got < cut if op == "before" else got > cut
        days = parse_number(target)
        if days is None:
            return False
        delta = (ref - got).total_seconds() / 86400.0
        if op == "within_days":
            return 0 <= delta <= days
        if op == "older_than_days":
            return delta > days
        return -days <= delta < 0  # next_days

    # Numbers
    if op in ("gt", "gte", "lt", "lte", "between"):
        got = parse_number(raw)
        if got is None:
            return False
        if op == "between":
            bounds = target if isinstance(target, (list, tuple)) else [None, None]
            low = parse_number(bounds[0] if len(bounds) > 0 else None)
            high = parse_number(bounds[1] if len(bounds) > 1 else None)
            if low is None or high is None:
                return False
            if low > high:
                low, high = high, low
            return low <= got <= high
        other = parse_number(target)
        if other is None:
            return False
        return {"gt": got > other, "gte": got >= other,
                "lt": got < other, "lte": got <= other}[op]

    # Text (numbers compare numerically when both sides look numeric)
    got_text = "" if raw is None else str(raw).strip()
    if op in ("eq", "neq"):
        other = "" if target is None else str(target).strip()
        a, b = parse_number(got_text), parse_number(other)
        same = (a == b) if (a is not None and b is not None) else (got_text.lower() == other.lower())
        return same if op == "eq" else not same
    if op in ("in", "not_in"):
        choices = {v.strip().lower() for v in _as_list(target)}
        hit = got_text.lower() in choices
        return hit if op == "in" else not hit
    if op == "contains":
        return str(target or "").strip().lower() in got_text.lower()
    if op == "not_contains":
        return str(target or "").strip().lower() not in got_text.lower()
    if op == "starts_with":
        return got_text.lower().startswith(str(target or "").strip().lower())
    if op == "ends_with":
        return got_text.lower().endswith(str(target or "").strip().lower())
    if op == "regex":
        try:
            return re.search(str(target or ""), got_text, re.I) is not None
        except re.error:
            return False
    return False


def evaluate(node, row, today=None):
    """Evaluate a condition tree. A missing or childless group matches all."""
    if not node:
        return True
    if node.get("type") == "cond":
        return eval_cond(node, row, today)
    children = node.get("children") or []
    if not children:
        return True
    op = (node.get("op") or "AND").upper()
    if op == "OR":
        result = any(evaluate(child, row, today) for child in children)
    else:
        result = all(evaluate(child, row, today) for child in children)
    return (not result) if node.get("negate") else result


def describe(node, depth=0):
    """Plain-English rendering, used in the UI and in exported rule docs."""
    if not node:
        return "all samples"
    if node.get("type") == "cond":
        kind = node.get("kind", "text")
        label, arity = OPERATORS.get(kind, OPERATORS["text"]).get(
            node.get("op"), (node.get("op"), 1))
        if arity == 0:
            return "%s %s" % (node.get("field"), label)
        if arity == "list":
            return "%s %s [%s]" % (node.get("field"), label, ", ".join(_as_list(node.get("value"))))
        if arity == 2:
            vals = node.get("value") or ["", ""]
            return "%s %s %s and %s" % (node.get("field"), label, vals[0], vals[1] if len(vals) > 1 else "")
        return "%s %s %s" % (node.get("field"), label, node.get("value"))
    children = node.get("children") or []
    if not children:
        return "all samples"
    joiner = " OR " if (node.get("op") or "AND").upper() == "OR" else " AND "
    body = joiner.join(describe(c, depth + 1) for c in children)
    if node.get("negate"):
        return "NOT (%s)" % body
    return ("(%s)" % body) if depth else body


def classify(statuses, row, today=None):
    """First matching status wins. Returns a status dict or None."""
    for status in statuses:
        if evaluate(status["conditions"], row, today):
            return status
    return None
