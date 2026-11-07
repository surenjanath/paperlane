"""Workflow diagram navigation with branch lanes.

Steps are stored flat (ordered). A step that lives inside a branch lane carries ``settings["lane"] =
{"branch": <branch step key>, "lane": <lane id>}``; steps without it are on the main line. Lanes nest: a branch
step inside a lane has its own lanes. Each branch entry (a lane) is::

    {"id", "label", "logic", "rules",
     "target": "lane" | "next" | "end" | <step key>,   # when the lane is chosen: run its steps / continue after
                                                      #   the branches / complete the workflow / go to a step
     "then": "merge" | "end" | <step key>}            # after the lane's last step: continue after the branches /
                                                      #   complete / go to a step

Older branches (no ``id``/``then``, target "next" or a step key) keep their previous meaning.
Functions accept the snapshot step dicts (``packet.steps``) or ``Step`` model instances.
"""
END = "__end__"


def _get(s, attr, default=None):
    if isinstance(s, dict):
        return s.get(attr, default)
    return getattr(s, attr, default)


def settings(s):
    return _get(s, "settings") or {}


def branches(s):
    return _get(s, "branches") or []


def lane_id(branch, index):
    return branch.get("id") or f"i{index}"


def lane_of(s):
    """(branch key, lane id) of the lane holding step ``s``; None on the main line."""
    ln = settings(s).get("lane")
    if isinstance(ln, dict) and ln.get("branch"):
        return ln["branch"], str(ln.get("lane"))
    return None


def _index(steps):
    return {_get(s, "key"): s for s in steps}


def children(steps, container):
    """Direct members of a container (None = main line, or (branch key, lane id)), in order."""
    by_key = _index(steps)
    out = []
    for s in steps:
        ln = lane_of(s)
        if ln is not None and ln[0] not in by_key:
            ln = None  # orphaned lane (its branch was deleted) → treat as main line
        if ln == container:
            out.append(s)
    return out


def descendants(steps, branch_key):
    """Keys of every step nested (at any depth) in the lanes of ``branch_key``."""
    found, frontier = set(), {branch_key}
    while frontier:
        nxt = set()
        for s in steps:
            ln = lane_of(s)
            if ln and ln[0] in frontier and _get(s, "key") not in found:
                found.add(_get(s, "key"))
                if _get(s, "kind") == "branch":
                    nxt.add(_get(s, "key"))
        frontier = nxt
    return found


def _lane(steps, container):
    branch_step = _index(steps).get(container[0])
    for i, b in enumerate(branches(branch_step) if branch_step is not None else []):
        if lane_id(b, i) == container[1]:
            return branch_step, b
    return branch_step, None


def successor(steps, key):
    """Key of the step that follows ``key`` once it (and, for a branch, its lanes) is done; END when finished."""
    by_key = _index(steps)
    s = by_key.get(key)
    if s is None:
        return END
    container = lane_of(s) if lane_of(s) and lane_of(s)[0] in by_key else None
    sibs = children(steps, container)
    keys = [_get(x, "key") for x in sibs]
    i = keys.index(key)
    if i + 1 < len(keys):
        return keys[i + 1]
    if container is None:
        return END
    return lane_exit(steps, container)


def lane_exit(steps, container):
    """Where a lane goes after its last step."""
    branch_step, b = _lane(steps, container)
    if branch_step is None:
        return END
    then = (b or {}).get("then") or "merge"
    if then == "end":
        return END
    if then != "merge" and then in _index(steps):
        return then
    return successor(steps, _get(branch_step, "key"))


def lane_entry(steps, container):
    kids = children(steps, container)
    return _get(kids[0], "key") if kids else lane_exit(steps, container)


def branch_target(steps, branch_step, index):
    """Step key to run when lane ``index`` of ``branch_step`` matches."""
    b = branches(branch_step)[index]
    target = b.get("target") or "next"
    if target == "lane":
        return lane_entry(steps, (_get(branch_step, "key"), lane_id(b, index)))
    if target == "end":
        return END
    if target in ("next", "merge"):
        return successor(steps, _get(branch_step, "key"))
    return target if target in _index(steps) else successor(steps, _get(branch_step, "key"))


def first(steps):
    kids = children(steps, None)
    return _get(kids[0], "key") if kids else END


def tree(steps, container=None):
    """Nested structure for drawing: [{"step": s, "lanes": [{"branch", "index", "id", "nodes"}]}]."""
    out = []
    for s in children(steps, container):
        node = {"step": s, "lanes": []}
        if _get(s, "kind") == "branch":
            for i, b in enumerate(branches(s)):
                lid = lane_id(b, i)
                node["lanes"].append({"branch": b, "index": i, "id": lid,
                                      "nodes": tree(steps, (_get(s, "key"), lid))})
        out.append(node)
    return out


def ensure_lane_ids(branch_list):
    """Pin an id on every branch entry; id-less (older) entries keep the positional id their lanes already use."""
    for i, b in enumerate(branch_list):
        if not b.get("id"):
            b["id"] = f"i{i}"
    return branch_list


def new_lane(label, target="lane"):
    import secrets

    return {"id": secrets.token_hex(4), "label": label, "logic": "and", "rules": [], "target": target, "then": "merge"}
