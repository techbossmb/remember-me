#!/usr/bin/env python3
"""remember.me map: the short-term layer over the raw log, built as an ORDERED REPLAY of events.

Storage per project (<root>/maps/<project>/):
  events.jsonl   append-only map edits. Each event: {"seq","at","src","by","op",...}. The map is
                 the replay of these events sorted by `at` (when the thing was SAID, not when it
                 was ingested). by = builder | user | dream.
  sources.json   the timeline: which raw logs belong to the project and when they happened, e.g.
                 {"sources": [{"file": "x.md", "start": "2026-09-15T00:00:00Z",
                   "end": "2026-09-17T23:00:00Z", "order_uncertain": true}]}
                 Messages whose header carries a timestamp use it; others are interpolated.
  graph.json     cache of the latest replay (for viewers); rebuilt on every edit.

Ops (fields besides seq/at/src/by):
  node.create  {id, type: project|thread|playbook, title, status, summary, parent?, aliases?, spans?, weight?}
  node.update  {id, title?, status?, summary?, aliases? (added), spans? (added), weight?}
  node.merge   {from, into}            node.move {id, parent}
  claim.add    {node, kind: decision|requirement|rejected|open, text, quote?, why?, spans?, id?}
  claim.supersede {id, reason?, new?: {kind, text, quote?, why?, spans?}}   (open questions: "resolved")
  edge.add     {src, dst, type, note?}  edge.remove {src, dst, type}
  rule.add     {id?, scope: user|project, text, playbook?, spans?}
  rule.supersede {rid, reason?, new: {text, scope?, playbook?, when?, spans?}}
  rule.update  {rid, when?, playbook?, scope?}   metadata only (trigger/playbook link); meaning changes use supersede
  Any op may carry "project": "<other>" to land in another project's map (cross-project update).

Ordering guarantees:
  - An event can never supersede a claim/rule stated AFTER it (importing an old conversation into
    a newer map adds history; it does not undo newer decisions). Such attempts are reported.
  - Nodes touched by a user event are pinned; dream events cannot change pinned nodes.

CLI (all take --root DIR and --until CUT, where CUT is an ISO time or a message like file.md#m0061;
with --until, the map and every tool only see what existed at that moment):
  memmap.py <p> render [--focus ...] [--budget N] [--ids]
  memmap.py <p> expand <node_id> ...
  memmap.py <p> spans <file#mA-mB> ...
  memmap.py <p> grep <regex> [--max N]
  memmap.py <p> chunks [--size N]          list replay chunks in time order
  memmap.py <p> chunk <N> [--size N]       print chunk N (the messages to replay next)
  memmap.py <p> op '<json>' [--src file#mID] [--by builder|user|dream]    append one event
  memmap.py <p> ops <file.json> [--by ...] apply a JSON list of ops (each may have its own "src")
  memmap.py <p> rebuild                    replay events -> graph.json, print warnings
  memmap.py <p> zoom <id>… [--q Q] [--recent N]  open threads the agent picked from the overview, with neighbours
  memmap.py <p> find <words>               keyword fallback when no thread in the overview fits (alias: recall)
  memmap.py <p> window <from> [<to>]       what happened in a time window (dates or ISO times)
  memmap.py <p> grep <regex> --from D --to D   search only a time window
  memmap.py <p> coverage [N]               messages the map can't reach (should trend to 0); N = one chunk
  memmap.py <p> validate
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

TOOL_PREFIX = ""  # e.g. "remember " when used through the CLI
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
EDGE_TYPES = {"contains", "pivoted_from", "revisits", "supersedes", "depends_on",
              "discovered_during", "contradicts", "related"}
HDR = re.compile(r"^## ([mL]\d+) · ([a-z]+)(?: · (\S+))?", re.M)
KINDS = {"decision": "decisions", "requirement": "requirements", "rejected": "rejected", "open": "open_questions", "item": "items"}

# ---------------------------------------------------------------- raw log + timeline

_LOG_CACHE = {}


def _parse_log(root, fname):
    key = (root, fname)
    if key not in _LOG_CACHE:
        text = open(os.path.join(root, "log", fname), encoding="utf-8").read()
        heads = list(HDR.finditer(text))
        out = []
        for i, h in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            chunk = text[h.end():end]
            body = chunk.split("\n", 1)[1] if "\n" in chunk else ""
            out.append((h.group(1), h.group(2), body.strip(), h.group(3) or ""))
        _LOG_CACHE[key] = out
    return _LOG_CACHE[key]


def log_messages(root, fname):
    """Ordered (msg_id, role, text) for a raw-log file."""
    return [(m, r, b) for m, r, b, _ in _parse_log(root, fname)]


def _num(mid):
    return int(mid[1:])


def _ts(s):
    s = s.replace("Z", "+00:00")
    if len(s) == 10:
        s += "T00:00:00+00:00"
    return dt.datetime.fromisoformat(s)


def _iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sources(root, project):
    p = os.path.join(root, "maps", project, "sources.json")
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))["sources"]
    g = os.path.join(root, "maps", project, "graph.json")  # legacy maps: logs list, no times
    logs = json.load(open(g, encoding="utf-8")).get("logs", []) if os.path.exists(g) else []
    return [{"file": f, "start": "2000-01-01", "end": "2000-01-02", "order_uncertain": True} for f in logs]


def msg_times(root, src):
    """{msg_id: iso time} for one source entry: header timestamps, else interpolated."""
    msgs = _parse_log(root, src["file"])
    nums = [_num(m[0]) for m in msgs] or [0]
    # piecewise-linear interpolation between known points: start, any "anchors" {msg_id: time}, end
    pts = {min(nums): _ts(src["start"]), max(nums): _ts(src["end"])}
    for k, v in (src.get("anchors") or {}).items():
        pts[_num(k)] = _ts(v)
    xs = sorted(pts)
    out = {}
    for mid, _, _, ts in msgs:
        if ts and re.match(r"\d{4}-\d\d-\d\dT", ts):
            out[mid] = ts if ts.endswith("Z") else ts + "Z"
            continue
        x = _num(mid)
        lo = max([p for p in xs if p <= x], default=xs[0])
        hi = min([p for p in xs if p >= x], default=xs[-1])
        frac = 0 if hi == lo else (x - lo) / (hi - lo)
        out[mid] = _iso(pts[lo] + (pts[hi] - pts[lo]) * frac)
    return out


def when(root, project, span):
    """Assertion time of a span = time of its first message (searching this project's sources,
    then any source of any project for cross-project spans)."""
    f, _, rng = span.partition("#")
    mid = (rng.split("-")[0] if rng else None)
    cands = [s for s in sources(root, project) if s["file"] == f]
    if not cands:
        for p in os.listdir(os.path.join(root, "maps")):
            cands += [s for s in sources(root, p) if s["file"] == f]
    if not cands:
        raise ValueError(f"span {span}: file not in any project's sources.json")
    times = msg_times(root, cands[0])
    if mid is None:
        return min(times.values())
    if mid not in times:
        raise ValueError(f"span {span}: no message {mid}")
    return times[mid]


def cut_time(root, project, until):
    if not until:
        return None
    return when(root, project, until) if "#" in until else _iso(_ts(until))


def visible(root, project, fname, mid, cut):
    if not cut:
        return True
    for s in sources(root, project):
        if s["file"] == fname:
            return msg_times(root, s).get(mid, "9999") <= cut
    return when(root, project, f"{fname}#{mid}") <= cut

# ---------------------------------------------------------------- events -> map (replay)


def _events_path(root, project):
    return os.path.join(root, "maps", project, "events.jsonl")


def read_events(root, project):
    p = _events_path(root, project)
    if not os.path.exists(p):
        return []
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def _lock(f):
    """Exclusive lock on an open file, released when it closes. POSIX flock; on Windows msvcrt."""
    try:
        import fcntl
        fcntl.flock(f, fcntl.LOCK_EX)
    except ImportError:
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)


def append_event(root, project, ev):
    target = ev.pop("project", None) or project
    os.makedirs(os.path.join(root, "maps", target), exist_ok=True)
    with open(_events_path(root, target), "a+", encoding="utf-8") as f:
        _lock(f)  # several builders/sessions may write at once
        f.seek(0)
        seqs = [json.loads(l)["seq"] for l in f if l.strip()]
        ev["seq"] = max(seqs, default=0) + 1
        if target != project:
            ev["origin_project"] = project
        f.seek(0, 2)
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return target, ev


def replay(root, project, cut=None):
    evs = sorted(read_events(root, project), key=lambda e: (e["at"], e["seq"]))
    nodes, edges, rules, claims, warn = {}, [], {}, {}, []
    order = []
    born = {}  # id -> when it was first stated, to explain references to the future
    for e in evs:
        for k in ("id", "rid", "cid"):
            if e.get(k) and e["op"] in ("node.create", "rule.add", "claim.add"):
                born.setdefault(e[k], e["at"])
        nw = e.get("new") or {}
        for k in ("rid", "cid"):
            if nw.get(k):
                born.setdefault(nw[k], e["at"])

    def node(i):
        return nodes.get(i)

    def addu(lst, xs):
        for x in xs or []:
            if x not in lst:
                lst.append(x)

    for e in evs:
        if cut and e["at"] > cut:
            break
        op, at, by = e["op"], e["at"], e.get("by", "builder")
        tgt = e.get("id") or e.get("node") or e.get("into") or e.get("src_node")
        if by == "dream" and tgt in nodes and nodes[tgt].get("pinned"):
            warn.append(f"#{e['seq']} {op}: {tgt} is pinned by the user; dream edit skipped")
            continue
        try:
            if op in ("node.create", "node.update"):
                n = nodes.get(e["id"])
                if n is None:
                    if op == "node.update":
                        raise KeyError(e["id"])
                    n = nodes[e["id"]] = {"id": e["id"], "type": e.get("type", "thread"), "title": "", "status": "active",
                                          "summary": "", "aliases": [], "spans": [], "weight": 0.5, "first_seen": at,
                                          "decisions": [], "requirements": [], "rejected": [], "open_questions": [], "items": []}
                    order.append(e["id"])
                    if e.get("parent"):
                        edges.append({"src": e["parent"], "dst": e["id"], "type": "contains"})
                for k in ("title", "status", "summary", "weight", "type"):
                    if k in e:
                        n[k] = e[k]
                addu(n["aliases"], e.get("aliases"))
                addu(n["spans"], e.get("spans") or ([e["src"]] if e.get("src") and "#" in e.get("src", "") else []))
                n["last_seen"] = at
            elif op == "node.merge":
                a, b = nodes[e["from"]], nodes[e["into"]]
                for k in KINDS.values():
                    b[k] += a[k]
                addu(b["aliases"], a["aliases"] + [a["title"]])
                addu(b["spans"], a["spans"])
                for x in edges:
                    x["src"] = e["into"] if x["src"] == e["from"] else x["src"]
                    x["dst"] = e["into"] if x["dst"] == e["from"] else x["dst"]
                edges[:] = [x for x in edges if x["src"] != x["dst"]]
                b.setdefault("merged", []).append({"id": a["id"], "title": a["title"], "at": at})
                del nodes[e["from"]]
                order.remove(e["from"])
                tgt = e["into"]
            elif op == "node.move":
                edges[:] = [x for x in edges if not (x["type"] == "contains" and x["dst"] == e["id"])]
                if e.get("parent"):
                    edges.append({"src": e["parent"], "dst": e["id"], "type": "contains"})
            elif op == "claim.add":
                n = nodes[e["node"]]
                c = {"id": e.get("cid") or f"C{e['seq']}", "text": e.get("text", e.get("option", "")), "at": at,
                     "spans": e.get("spans") or ([e["src"]] if e.get("src") else [])}
                for k in ("quote", "why"):
                    if e.get(k):
                        c[k] = e[k]
                n[KINDS[e["kind"]]].append(c)
                claims[c["id"]] = (n["id"], c)
            elif op == "claim.supersede":
                nid, c = claims[e["id"]]
                if c["at"] > at:
                    warn.append(f"#{e['seq']} claim.supersede {e['id']}: that claim was stated later ({c['at']}) "
                                f"than this event ({at}); kept as history instead")
                    continue
                newid = None
                if e.get("new"):
                    nw = dict(e["new"])
                    nn = nodes[nw.pop("node", nid)]
                    newid = nw.pop("cid", None) or f"C{e['seq']}"
                    nc = {"id": newid, "text": nw.get("text", ""), "at": at,
                          "spans": nw.get("spans") or ([e["src"]] if e.get("src") else []), "supersedes": c["id"]}
                    for k in ("quote", "why"):
                        if nw.get(k):
                            nc[k] = nw[k]
                    nn[KINDS[nw.get("kind", "decision")]].append(nc)
                    claims[newid] = (nn["id"], nc)
                c["superseded_by"] = newid or ("resolved" if e.get("reason") is None else e["reason"])
                c["superseded_at"] = at
                if e.get("reason"):
                    c["superseded_reason"] = e["reason"]
                tgt = nid
            elif op == "edge.add":
                if e["type"] not in EDGE_TYPES:
                    raise ValueError(f"bad edge type {e['type']}")
                edges.append({"src": e["src_node"] if "src_node" in e else e["from"], "dst": e["to"], "type": e["type"],
                              **({"note": e["note"]} if e.get("note") else {})})
            elif op == "edge.remove":
                s = e.get("src_node", e.get("from"))
                edges[:] = [x for x in edges if not (x["src"] == s and x["dst"] == e["to"] and x["type"] == e["type"])]
            elif op == "rule.add":
                rid = e.get("rid") or f"R{len(rules) + 1}"
                rules[rid] = {"id": rid, "scope": e.get("scope", "project"), "text": e["text"], "stated": at,
                              "spans": e.get("spans") or ([e["src"]] if e.get("src") else []), "status": "active"}
                for k in ("playbook", "when"):
                    if e.get(k):
                        rules[rid][k] = e[k]
            elif op == "rule.update":
                r = rules[e["rid"]]
                for k in ("when", "playbook", "scope"):
                    if k in e:
                        r[k] = e[k]
            elif op == "rule.supersede":
                old = rules[e["rid"]]
                if old["stated"] > at:
                    warn.append(f"#{e['seq']} rule.supersede {e['rid']}: that rule was stated later ({old['stated']}); kept as history")
                    continue
                old["status"], old["superseded_at"] = "superseded", at
                if e.get("reason"):
                    old["superseded_reason"] = e["reason"]
                nw = e.get("new")
                if nw:
                    rid = nw.get("rid") or f"R{len(rules) + 1}"
                    rules[rid] = {"id": rid, "scope": nw.get("scope", old["scope"]), "text": nw["text"], "stated": at,
                                  "spans": nw.get("spans") or ([e["src"]] if e.get("src") else []), "status": "active",
                                  "supersedes": old["id"]}
                    for k in ("playbook", "when"):
                        if nw.get(k) or old.get(k):
                            rules[rid][k] = nw.get(k) or old.get(k)
                    old["superseded_by"] = rid
            else:
                raise ValueError(f"unknown op {op}")
            if by == "user" and tgt in nodes:
                nodes[tgt]["pinned"] = True
        except KeyError as ex:
            k = ex.args[0]
            if k in born and born[k] > at:
                warn.append(f"#{e['seq']} {op}: {k} was stated later ({born[k]}) than this message ({at}); "
                            f"an older statement can't change it, so it is ignored")
            else:
                warn.append(f"#{e['seq']} {op}: unknown id {k}")
        except ValueError as ex:
            warn.append(f"#{e['seq']} {op}: {ex}")

    srcs = sources(root, project)
    g = {"project": project, "logs": [s["file"] for s in srcs], "built": _iso(dt.datetime.now(dt.timezone.utc)),
         "as_of": cut or (evs[-1]["at"] if evs else ""), "nodes": [nodes[i] for i in order if i in nodes],
         "edges": edges, "rules": list(rules.values()), "warnings": warn}
    return g


def load(root, project, cut=None):
    if os.path.exists(_events_path(root, project)) or os.path.exists(os.path.join(root, "maps", project, "sources.json")):
        g = replay(root, project, cut)
    else:
        with open(os.path.join(root, "maps", project, "graph.json"), encoding="utf-8") as f:
            g = json.load(f)
    g["_by_id"] = {n["id"]: n for n in g.get("nodes", [])}
    g["_cut"] = cut
    g["_root"] = root
    return g


def save_cache(root, project):
    g = replay(root, project)
    with open(os.path.join(root, "maps", project, "graph.json"), "w", encoding="utf-8") as f:
        json.dump(g, f, indent=1, ensure_ascii=False)
    return g

# ---------------------------------------------------------------- span tools


def read_span(root, g, span, max_chars=12000):
    fname, _, rng = span.partition("#")
    msgs = _parse_log(root, fname)
    if rng:
        a, _, b = rng.partition("-")
        lo, hi = _num(a), _num(b or a)
        msgs = [m for m in msgs if lo <= _num(m[0]) <= hi]
    cut = g.get("_cut")
    hidden = [m for m in msgs if not visible(root, g["project"], fname, m[0], cut)]
    msgs = [m for m in msgs if m not in hidden]
    out = [f"=== {span}"] + [f"--- {fname}#{mid} [{role}]{' ' + ts if ts else ''}\n{body}" for mid, role, body, ts in msgs]
    if hidden:
        out.append(f"[{len(hidden)} message(s) in this span are after the current time cut and not shown]")
    s = "\n".join(out)
    return s if len(s) <= max_chars else s[:max_chars] + f"\n[... truncated at {max_chars} chars; request a narrower span]"


def grep(root, g, pattern, max_hits=40, start=None, end=None):
    rx = re.compile(pattern, re.I)
    hits = []
    times = {}
    if start or end:
        for s in sources(root, g["project"]):
            times[s["file"]] = msg_times(root, s)
    lo = _iso(_ts(start)) if start else ""
    hi = _iso(_ts(end) + dt.timedelta(days=1)) if end and len(end) == 10 else (_iso(_ts(end)) if end else "9999")
    for fname in g.get("logs", []):
        for mid, role, body, _ in _parse_log(root, fname):
            if not visible(root, g["project"], fname, mid, g.get("_cut")):
                continue
            if times and not (lo <= times.get(fname, {}).get(mid, "") < hi):
                continue
            for line in body.split("\n"):
                if rx.search(line):
                    hits.append(f"{fname}#{mid} [{role}]: {line.strip()[:220]}")
    more = len(hits) - max_hits
    return "\n".join(hits[:max_hits]) + (f"\n[... {more} more hits; narrow the pattern]" if more > 0 else "") or "(no hits)"


def span_msgs(root, span):
    f, _, rng = span.partition("#")
    try:
        msgs = _parse_log(root, f)
    except FileNotFoundError:
        return set()
    if not rng:
        return {(f, m[0]) for m in msgs}
    a, _, b = rng.partition("-")
    lo, hi = _num(a), _num(b or a)
    return {(f, m[0]) for m in msgs if lo <= _num(m[0]) <= hi}

# ---------------------------------------------------------------- render (the short-term view)


def _children(g):
    kids, has_parent = {}, set()
    for e in g.get("edges", []):
        if e["type"] == "contains" and e["dst"] in g["_by_id"] and e["src"] in g["_by_id"]:
            kids.setdefault(e["src"], []).append(e["dst"])
            has_parent.add(e["dst"])
    roots = [n["id"] for n in g["nodes"] if n["id"] not in has_parent]
    return kids, roots


def counts(g):
    """{node_id: (messages behind it incl. sub-nodes, direct sub-nodes)}"""
    kids, _ = _children(g)
    root = g.get("_root", ROOT)
    own = {n["id"]: set().union(*[span_msgs(root, s) for s in n.get("spans", [])]) if n.get("spans") else set()
           for n in g["nodes"]}
    memo = {}

    def tot(i, seen=()):
        if i in memo:
            return memo[i]
        s = set(own.get(i, set()))
        for c in kids.get(i, []):
            if c not in seen:
                s |= tot(c, seen + (i,))
        memo[i] = s
        return s

    return {n["id"]: (len(tot(n["id"])), len(kids.get(n["id"], []))) for n in g["nodes"]}


def last_active(g):
    """{node_id: ISO time of the latest visible message behind it, incl. sub-threads}"""
    if "_last" in g:
        return g["_last"]
    root = g.get("_root", ROOT)
    times = {}
    for s in sources(root, g["project"]):
        for mid, ts in msg_times(root, s).items():
            times[(s["file"], mid)] = ts
    kids, _ = _children(g)
    own = {}
    for n in g["nodes"]:
        sp = list(n.get("spans", []))
        for k in KINDS.values():
            for x in n.get(k) or []:
                if isinstance(x, dict):
                    sp += x.get("spans") or []
        ms = set().union(*[span_msgs(root, s) for s in sp]) if sp else set()
        own[n["id"]] = max([times[m] for m in ms if m in times and visible(root, g["project"], m[0], m[1], g.get("_cut"))],
                           default="")
    memo = {}

    def tot(i, seen=()):
        if i not in memo:
            memo[i] = max([own.get(i, "")] + [tot(c, seen + (i,)) for c in kids.get(i, []) if c not in seen])
        return memo[i]

    g["_last"] = {n["id"]: tot(n["id"]) for n in g["nodes"]}
    return g["_last"]


def _focus_set(g, focus):
    if not focus:
        return set()
    ids = {f.strip() for f in focus.split(",") if f.strip() in g["_by_id"]}
    words = [w.lower() for w in re.findall(r"\w+", focus) if len(w) > 2 and w not in g["_by_id"]]
    for n in g["nodes"]:
        hay = " ".join([n.get("title", ""), n.get("summary", ""), " ".join(n.get("aliases", []))]).lower()
        if words and sum(w in hay for w in words) >= max(1, len(words) // 2):
            ids.add(n["id"])
    for e in g.get("edges", []):
        if (e["src"] in ids or e["dst"] in ids) and e["type"] != "contains":
            ids |= {e["src"], e["dst"]}
    return ids


def _live(xs):
    return [x for x in xs or [] if not (isinstance(x, dict) and x.get("superseded_by"))]


def node_line(n, cnt=None):
    st, oq = n.get("status", ""), len(_live(n.get("open_questions")))
    s = f"[{n['id']}] ({st}{', playbook' if n.get('type') == 'playbook' else ''}) {n['title']} — {n.get('summary', '')}"
    if n.get("type") == "list":
        its = n.get("items") or []
        s += f"  ·LIST {len(_live(its))} open / {len(its)} total (last change {max([x.get('superseded_at') or x.get('at', '') for x in its], default='?')[:10]})"
    if cnt:
        s += f"  ·{cnt[0]} msgs" + (f" ·{cnt[1]} sub" if cnt[1] else "")
    if oq:
        s += f"  ·open:{oq}"
    if n.get("aliases"):
        s += f"  ·aka: {', '.join(n['aliases'][:4])}"
    return s


def node_detail(n, indent="    ", ids=False):
    out = []
    for label, key in (("item", "items"), ("decided", "decisions"), ("requires", "requirements"), ("rejected", "rejected"), ("open", "open_questions")):
        for x in _live(n.get(key)):
            t = x.get("text") or x.get("option", "") if isinstance(x, dict) else str(x)
            if key == "rejected" and isinstance(x, dict) and x.get("why"):
                t += f" (why: {x['why']})"
            if isinstance(x, dict) and x.get("quote"):
                t += f' — "{x["quote"]}"'
            out.append(f"{indent}{label}{' ' + x['id'] if ids and isinstance(x, dict) and x.get('id') else ''}: {t}")
    return out


HEADER = ("The thread list below is your overview of everything in this project. To answer or act, pick EVERY thread "
          "that could bear on it by meaning (not by matching words; a question about losing data is about backups) and "
          "open them together in one call: zoom <id> <id> … --q \"<the question>\". It returns each thread with verbatim "
          "evidence and where it stands, its parent/sub-threads/links, and raw-log messages outside those threads that "
          "match the question. For 'where were we' questions: zoom --recent 5. Use find <words> only when no thread fits. "
          "For 'what's on the list' questions, enumerate the list node completely, give the count, and offer to scan "
          "the log since its last change for items mentioned but never added. Only say something was never discussed "
          "after a full scan of the relevant time window (window <from> <to>). "
          "This map is an index, not the record: if the log disagrees with the map, the log wins; say so. "
          "When you are about to DO work a rule's trigger covers, zoom its playbook and copy every step into your plan as "
          "a numbered checklist; answering questions doesn't need playbooks.")


def render(g, focus=None, budget=9000, ids=False):
    """Compress gradually until it fits: 0 everything; 1 drop the Links section; 2 resolved/reference
    threads become title + counts; 3 claim details only for focused/active threads, capped at 4 each;
    4 every non-focused thread is title + counts; 5 no claim details, no rule dates."""
    fset, (kids, roots), by, cnt = _focus_set(g, focus), _children(g), g["_by_id"], counts(g)
    last = last_active(g)

    def build(level):
        lines = [f"# Memory map: {g['project']}  (as of {g.get('as_of') or g.get('built', '?')}; tools: {TOOL_PREFIX}zoom <id> [<id>…] --q \"<question>\" | {TOOL_PREFIX}zoom --recent N, "
                 f"{TOOL_PREFIX}spans <file#mA-mB>, {TOOL_PREFIX}grep <regex> [--from D --to D], {TOOL_PREFIX}window <from> <to>, {TOOL_PREFIX}find <words>)", HEADER]
        rules = [r for r in g.get("rules", []) if r.get("status", "active") == "active"]
        if rules:
            lines.append("## Rules — always apply")
            for r in rules:
                t = f"- [{r['id']}] ({r.get('scope', 'project')}) {r['text']}"
                if r.get("playbook"):
                    t += (f" → when {r['when']}: follow playbook {r['playbook']}" if r.get("when")
                          else f" → playbook {r['playbook']}")
                if level < 5 and r.get("stated"):
                    t += f" (since {r['stated'][:10]})"
                lines.append(t)
        lines.append("## Threads")
        seen = set()

        def walk(nid, depth):
            if nid in seen or nid not in by:
                return
            seen.add(nid)
            n, pad = by[nid], "  " * depth
            st = n.get("status")
            live = st in ("active", "paused") or n.get("type") in ("project", "list")
            focused = nid in fset or (not fset and live and n.get("type") != "playbook")
            short = (level >= 2 and not live and nid not in fset) or (level >= 4 and not focused) or level >= 6
            if short:
                line = f"[{n['id']}] ({st}{', ' + n['type'] if n.get('type') in ('playbook', 'list') else ''}) {n['title']}  ·{cnt[nid][0]} msgs"
                if last.get(nid):
                    line += f" ·last {last[nid][:10]}"
                if n.get("type") == "list":
                    line += f" ·LIST {len(_live(n.get('items')))} open"
            else:
                line = node_line(n, cnt.get(nid)) + (f"  ·last {last[nid][:10]}" if last.get(nid) else "")
            lines.append(pad + "- " + line)
            if focused and level < 5:
                det = node_detail(n, ids=ids)
                if level >= 3 and n.get("type") != "list":
                    det = det[:4] + ([f"{pad}    … {len(det) - 4} more (expand {nid})"] if len(det) > 4 else [])
                lines.extend(pad + l for l in det)
            for c in kids.get(nid, []):
                walk(c, depth + 1)

        for r in roots:
            walk(r, 0)
        other = [e for e in g.get("edges", []) if e["type"] != "contains"]
        if other and level < 1:
            lines.append("## Links")
            lines += [f"- {e['src']} --{e['type']}--> {e['dst']}" + (f" ({e['note']})" if e.get("note") else "") for e in other]
        return "\n".join(lines)

    out = ""
    for level in range(7):  # 6: every thread is title + counts; the overview must always list every thread
        out = build(level)
        if len(out) <= budget:
            return out
    return out[:budget] + "\n[... map truncated; use expand/grep/window]"


STOP = set("""a an and are as at be been but by can could did do does done for from had has have how i if in into is it
its just me my no not of on or our so that the their them then there these they this to was we were what when where which
who why will with would you your yet any all about after again also back did didnt dont get got lets let us ok yes""".split())


def _words(s):
    return {w for w in re.findall(r"[a-z0-9_.\-]+", (s or "").lower()) if len(w) > 2 and w not in STOP}


def _visible_msgs(root, g, spans, limit=8):
    out, seen = [], set()
    for s in spans:
        f, _, rng = s.partition("#")
        try:
            msgs = _parse_log(root, f)
        except FileNotFoundError:
            continue
        if rng:
            a, _, b = rng.partition("-")
            lo, hi = _num(a), _num(b or a)
            msgs = [m for m in msgs if lo <= _num(m[0]) <= hi]
        for mid, role, body, _ in msgs:
            if (f, mid) in seen or not visible(root, g["project"], f, mid, g.get("_cut")):
                continue
            seen.add((f, mid))
            out.append((f, mid, role, body))
            if len(out) >= limit:
                return out
    return out


def evidence(root, g, claim, maxlen=240):
    """Best-matching verbatim excerpt (with its message ref) from the claim's own spans."""
    want = _words(claim.get("text", "")) | _words(claim.get("quote", ""))
    best = (0, None)
    for f, mid, role, body in _visible_msgs(root, g, claim.get("spans") or []):
        for piece in re.split(r"(?<=[.!?])\s+|\n+", body):
            sc = len(want & _words(piece))
            if sc > best[0] and len(piece) > 20:
                best = (sc, f"{f}#{mid} [{role}]: \"{piece.strip()[:maxlen]}\"")
    return best[1]


def stop_point(root, g, n):
    """Where a thread's conversation last was, and what the next message (any thread) turned to."""
    root = g.get("_root", root)
    times = {}
    for s in sources(root, g["project"]):
        for mid, ts in msg_times(root, s).items():
            times[(s["file"], mid)] = ts
    mine = [m for m in set().union(*[span_msgs(root, s) for s in n.get("spans", [])] or [set()])
            if m in times and visible(root, g["project"], m[0], m[1], g.get("_cut"))]
    if not mine:
        return None
    last = max(mine, key=lambda m: times[m])
    tl = timeline(root, g["project"], g.get("_cut"))
    idx = next((i for i, r in enumerate(tl) if (r[1], r[2]) == last), None)
    body = next((r[4] for r in tl if (r[1], r[2]) == last), "")
    out = f"last: {last[0]}#{last[1]} {times[last][:16]}: \"{body[:160].replace(chr(10), ' ')}\""
    if idx is not None and idx + 1 < len(tl):
        nx = tl[idx + 1]
        out += f"\n    next message: {nx[1]}#{nx[2]} [{nx[3]}]: \"{nx[4][:160].replace(chr(10), ' ')}\""
    return out


def expand(g, nid, compact=False):
    n = g["_by_id"].get(nid)
    if not n:
        return f"no node {nid}"
    root = g.get("_root", ROOT)
    c = counts(g).get(nid, (0, 0))
    out = (["PLAYBOOK (when doing this kind of work, copy every step into your plan as a numbered checklist):"]
           if n.get("type") == "playbook" else [])
    out += [node_line(n, c)]
    if not compact:
        out.append(f"  spans: {', '.join(n.get('spans', []))}" + ("  ·pinned by user" if n.get("pinned") else ""))
    for label, key in (("items", "items"), ("steps" if n.get("type") == "playbook" else "requirements", "requirements"),
                       ("decisions", "decisions"), ("rejected", "rejected"), ("open questions", "open_questions")):
        xs = n.get(key) or []
        live, old = [x for x in xs if not x.get("superseded_by")], [x for x in xs if x.get("superseded_by")]
        if not xs:
            continue
        out.append(f"  {label}:")
        for x in live:
            t = x.get("text", "") + (f" (why: {x['why']})" if x.get("why") else "")
            out.append(f"    - [{x.get('id', '')}] {t}" + (f"  ({x['at'][:10]})" if x.get("at") else ""))
            ev = evidence(root, g, x)
            if ev:
                out.append(f"        evidence: {ev}")
        for x in old:
            out.append(f"    - (superseded {x.get('superseded_at', '')[:10]}"
                       + (f", {x['superseded_reason']}" if x.get("superseded_reason") else "") + f") {x.get('text', '')[:140]}")
    if n.get("status") in ("paused", "active") and n.get("type") not in ("project", "list"):
        sp = stop_point(root, g, n)
        if sp:
            out.append("  where it stands in the log: " + sp)
    for m in n.get("merged", []):
        out.append(f"  absorbed: {m['id']} '{m['title']}' at {m['at']}")
    for e in g.get("edges", []):
        if nid in (e["src"], e["dst"]) and e["type"] != "contains":
            other = e["dst"] if e["src"] == nid else e["src"]
            out.append(f"  link: {e['src']} --{e['type']}--> {e['dst']}  ({g['_by_id'].get(other, {}).get('title', other)})")
    kids = [e["dst"] for e in g.get("edges", []) if e["type"] == "contains" and e["src"] == nid]
    if kids:
        out.append("  sub-threads: " + ", ".join(f"{k} {g['_by_id'].get(k, {}).get('title', '')}" for k in kids))
    return "\n".join(out)


def zoom(g, ids, question=None, recent=0, budget=40000):
    """The agent picks threads from the overview; open them together with their neighbourhood."""
    root = g.get("_root", ROOT)
    by, (kids, _), last = g["_by_id"], _children(g), last_active(g)
    ids = [i for i in ids if i in by]
    if recent:
        live = [n["id"] for n in g["nodes"] if n.get("type") not in ("project", "playbook") and last.get(n["id"])]
        live.sort(key=lambda i: last[i], reverse=True)
        ids += [i for i in live[:recent] if i not in ids]
    parent = {c: p for p, cs in kids.items() for c in cs}
    out = [f"# zoom: {' '.join(ids) or '(nothing)'}" + (f"  — q: {question}" if question else "")]
    if recent:
        out.append("most recently active first: " + ", ".join(f"{i} (last {last[i][:16]})" for i in ids[-recent:]))
    covered, near = set(), []
    for nid in ids:
        out.append(f"## {nid}" + (f"  (inside {parent[nid]} {by[parent[nid]]['title']})" if parent.get(nid) in by else ""))
        out.append(expand(g, nid, compact=True))
        for s in by[nid].get("spans", []):
            covered |= span_msgs(root, s)
        for c in kids.get(nid, []):
            near.append((c, f"sub-thread of {nid}"))
        for e in g.get("edges", []):
            if e["type"] != "contains" and nid in (e["src"], e["dst"]):
                o = e["dst"] if e["src"] == nid else e["src"]
                near.append((o, f"{e['src']} {e['type']} {e['dst']}" + (f" ({e['note']})" if e.get("note") else "")))
    seen, nb = set(ids), []
    for o, why in near:
        if o in by and o not in seen:
            seen.add(o)
            nb.append(f"- {node_line(by[o], None)}  ·last {last.get(o, '')[:10]}  [{why}]")
    if nb:
        out.append("## neighbours (not opened; zoom them if they matter)")
        out += nb
    if question:
        q, hits = _words(question), []
        for _, f, mid, role, body, _ in timeline(root, g["project"], g.get("_cut")):
            if (f, mid) not in covered:
                sc = len(q & _words(body))
                if sc >= 2:
                    hits.append((sc, f, mid, role, body))
        hits.sort(key=lambda h: -h[0])
        if hits:
            out.append("## raw-log messages outside these threads that also match the question")
            out += [f"- {f}#{mid} [{role}]: {body[:220].replace(chr(10), ' ')}" for _, f, mid, role, body in hits[:6]]
    s = "\n".join(out)
    return s if len(s) <= budget else s[:budget] + "\n[... truncated; zoom fewer ids]"


def recall(g, question, budget=16000, max_nodes=7):
    """One call: the threads most relevant to the question, expanded with verbatim evidence and where
    they stand, plus raw-log hits the map may have missed."""
    root = g.get("_root", ROOT)
    q = _words(question)
    scored = []
    for n in g["nodes"]:
        if n.get("type") == "project":
            continue
        head = _words(n.get("title")) | _words(" ".join(n.get("aliases", [])))
        body = _words(n.get("summary"))
        for k in KINDS.values():
            for x in n.get(k) or []:
                body |= _words(x.get("text", "")) | _words(x.get("quote", ""))
        sc = 3 * len(q & head) + len(q & body)
        if sc:
            scored.append((sc, n["id"]))
    scored.sort(reverse=True)
    top = [i for s, i in scored[:max_nodes] if s >= max(2, scored[0][0] // 4)] if scored else []
    out = [f"# recall: {question}", f"matched threads: {', '.join(top) or 'none'}"
           + (" (expand others by id if needed)" if len(scored) > len(top) else "")]
    rules = [r for r in g.get("rules", []) if r.get("status") == "active" and q & _words(r["text"] + " " + r.get("when", ""))]
    if rules:
        out.append("## relevant rules")
        out += [f"- [{r['id']}] {r['text']}" + (f" (when {r['when']}: playbook {r['playbook']})" if r.get("playbook") else "") for r in rules]
    covered = set()
    for nid in top:
        out.append("## " + nid)
        out.append(expand(g, nid, compact=True))
        for s in g["_by_id"][nid].get("spans", []):
            covered |= span_msgs(root, s)
    hits = []
    for t, f, mid, role, body, _ in timeline(root, g["project"], g.get("_cut")):
        if (f, mid) in covered:
            continue
        sc = len(q & _words(body))
        if sc >= 2:
            hits.append((sc, f, mid, role, body))
    hits.sort(key=lambda h: -h[0])
    if hits:
        out.append("## raw-log messages outside these threads that also match")
        out += [f"- {f}#{mid} [{role}]: {body[:200].replace(chr(10), ' ')}" for _, f, mid, role, body in hits[:5]]
    s = "\n".join(out)
    return s if len(s) <= budget else s[:budget] + "\n[... truncated; expand specific ids]"


# ---------------------------------------------------------------- replay chunks (for the builder)


def timeline(root, project, cut=None):
    """All messages of the project's sources in assertion order: [(time, file, mid, role, text)]."""
    rows = []
    for s in sources(root, project):
        times = msg_times(root, s)
        for mid, role, body, _ in _parse_log(root, s["file"]):
            if cut and times[mid] > cut:
                continue
            rows.append((times[mid], s["file"], mid, role, body, bool(s.get("order_uncertain"))))
    rows.sort(key=lambda r: (r[0], r[1], _num(r[2])))
    return rows


def chunk_list(root, project, size=20):
    tl = timeline(root, project)
    return [tl[i:i + size] for i in range(0, len(tl), size)]


def show_chunk(root, project, n, size=20):
    ch = chunk_list(root, project, size)
    if n >= len(ch):
        return f"no chunk {n}; there are {len(ch)}"
    out = [f"# Replay chunk {n} of {len(ch) - 1} for {project} — apply ops in this order; set each op's src to the message it comes from"]
    for t, f, mid, role, body, unc in ch[n]:
        out.append(f"--- {f}#{mid} [{role}] {t}{' (time approximate)' if unc else ''}\n{body}")
    return "\n".join(out)

# ---------------------------------------------------------------- coverage and time windows


def covered_ids(g):
    """Every (file, msg) reachable from the map: node spans, claim spans, rule spans."""
    root = g.get("_root", ROOT)
    spans = []
    for n in g["nodes"]:
        spans += n.get("spans", [])
        for k in KINDS.values():
            for x in n.get(k) or []:
                spans += x.get("spans", []) if isinstance(x, dict) else []
    for r in g.get("rules", []):
        spans += r.get("spans", [])
    out = set()
    for s in spans:
        out |= span_msgs(root, s)
    return out


def coverage(root, g, min_chars=80, chunk=None, size=20):
    """Messages the map can't reach. Short messages (acks, 'ok') are ignored. chunk=N limits to one replay chunk."""
    cov, rows, miss = covered_ids(g), timeline(root, g["project"], g.get("_cut")), []
    if chunk is not None:
        rows = rows[chunk * size:(chunk + 1) * size]
    big = [r for r in rows if len(r[4]) >= min_chars]
    for t, f, mid, role, body, _ in big:
        if (f, mid) not in cov:
            miss.append(f"{f}#{mid} [{role}] {t[:16]}: {body[:100].replace(chr(10), ' ')}")
    pct = 100 * (1 - len(miss) / max(1, len(big)))
    return f"coverage: {pct:.0f}% of {len(big)} substantive messages are reachable from the map; {len(miss)} are not" + \
        ("" if not miss else "\n" + "\n".join(miss))


def window(root, g, start, end):
    """What happened between two times: messages per source, and map changes grouped by node."""
    a, b = _iso(_ts(start)), _iso(_ts(end) + (dt.timedelta(days=1) if len(end) == 10 else dt.timedelta(0)))
    rows = [r for r in timeline(root, g["project"]) if a <= r[0] < b]
    out = [f"# {g['project']} between {a[:16]} and {b[:16]}: {len(rows)} messages"]
    per = {}
    for t, f, mid, *_ in rows:
        per.setdefault(f, []).append(mid)
    for f, ids in per.items():
        out.append(f"- {f}: {len(ids)} messages ({ids[0]}–{ids[-1]})  → spans {f}#{ids[0]}-{ids[-1]}")
    evs = [e for e in read_events(root, g["project"]) if a <= e["at"] < b]
    touched = {}
    for e in evs:
        nid = e.get("node") or e.get("id") or e.get("into") or e.get("from") or ("rules" if e["op"].startswith("rule") else None)
        if e["op"] == "claim.supersede" and e.get("id"):
            nid = e["id"].split(".")[0]
        touched.setdefault(nid or "?", []).append(e)
    if touched:
        out.append("## Map changes in this window")
    for nid, es in touched.items():
        title = g["_by_id"].get(nid, {}).get("title", nid)
        bits = []
        for e in es:
            if e["op"] == "claim.add":
                bits.append(f"+{e.get('kind')}: {e.get('text', '')[:90]}")
            elif e["op"] == "claim.supersede":
                bits.append(f"~superseded {e['id']}" + (f" → {e['new'].get('text', '')[:70]}" if e.get("new") else ""))
            elif e["op"].startswith("rule"):
                bits.append(f"{e['op']}: {(e.get('text') or (e.get('new') or {}).get('text', ''))[:90]}")
            elif e["op"] == "node.create":
                bits.append("created")
            elif e["op"] == "edge.add":
                bits.append(f"{e.get('type')} → {e.get('to')}")
        out.append(f"- [{nid}] {title}: " + ("; ".join(bits[:6]) or f"{len(es)} edits") + (f" (+{len(bits) - 6} more)" if len(bits) > 6 else ""))
    if not rows:
        out.append("(nothing in the raw log in this window)")
    return "\n".join(out)

# ---------------------------------------------------------------- validate


def validate(root, g):
    errs = list(g.get("warnings", []))
    ids = set(g["_by_id"])
    for e in g.get("edges", []):
        if e["src"] not in ids or e["dst"] not in ids:
            errs.append(f"edge to unknown node: {e}")
    for n in g["nodes"]:
        for s in n.get("spans", []):
            if not span_msgs(root, s):
                errs.append(f"{n['id']}: span resolves to no messages: {s}")
    for r in g.get("rules", []):
        if r.get("playbook") and r["playbook"] not in ids:
            errs.append(f"{r['id']}: playbook {r['playbook']} is not a node")
    head = "OK" if not errs else "ISSUES"
    return f"{head}: {len(ids)} nodes, {len(g.get('edges', []))} edges, {len([r for r in g.get('rules', []) if r['status'] == 'active'])} active rules" + \
        ("" if not errs else "\n" + "\n".join(errs))

# ---------------------------------------------------------------- CLI


def apply_ops(root, project, ops, by="builder", default_src=None):
    out = []
    for op in ops:
        ev = dict(op)
        src = ev.get("src") or default_src
        if by == "user" and not src:
            ev["at"] = _iso(dt.datetime.now(dt.timezone.utc))
        elif not ev.get("at"):
            if not src:
                raise SystemExit(f"op needs a src (the message it comes from): {op}")
            ev["at"] = when(root, project, src)
        if src:
            ev["src"] = src
        ev["by"] = ev.get("by", by)
        out.append(append_event(root, project, ev))
    for target in {t for t, _ in out}:
        g = save_cache(root, target)
        for w in g["warnings"][-5:]:
            print("warning:", w)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("cmd", choices=["render", "expand", "spans", "grep", "validate", "chunks", "chunk", "op", "ops", "rebuild",
                                    "window", "coverage", "recall", "find", "zoom"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--focus")
    ap.add_argument("--budget", type=int, default=9000)
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--size", type=int, default=20)
    ap.add_argument("--ids", action="store_true", help="show claim ids (for builders)")
    ap.add_argument("--src")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--by", default="builder", choices=["builder", "user", "dream"])
    ap.add_argument("--until", default=os.environ.get("REMEMBER_UNTIL"))
    ap.add_argument("-i", action="store_true", help="accepted for habit; grep is always case-insensitive")
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--q", help="zoom: the question, for raw-log hits outside the opened threads")
    ap.add_argument("--recent", type=int, default=0, help="zoom: also open the N most recently active threads")
    a = ap.parse_args()
    root, p = a.root, a.project
    if a.cmd == "op":
        for t, ev in apply_ops(root, p, [json.loads(" ".join(a.args))], a.by, a.src):
            print(f"ok: {t} #{ev['seq']} {ev['op']} at {ev['at']}")
        return
    if a.cmd == "ops":
        for t, ev in apply_ops(root, p, json.load(open(a.args[0], encoding="utf-8")), a.by, a.src):
            print(f"ok: {t} #{ev['seq']} {ev['op']} at {ev['at']}")
        return
    if a.cmd == "rebuild":
        g = save_cache(root, p)
        print(validate(root, {**g, "_by_id": {n["id"]: n for n in g["nodes"]}}))
        return
    if a.cmd == "chunks":
        for i, ch in enumerate(chunk_list(root, p, a.size)):
            print(f"chunk {i}: {ch[0][0]} → {ch[-1][0]}  ({len(ch)} msgs: {ch[0][1]}#{ch[0][2]} … {ch[-1][1]}#{ch[-1][2]})")
        return
    if a.cmd == "chunk":
        print(show_chunk(root, p, int(a.args[0]), a.size))
        return
    g = load(root, p, cut_time(root, p, a.until))
    if a.cmd == "render":
        print(render(g, a.focus, a.budget, a.ids))
    elif a.cmd == "expand":
        print("\n\n".join(expand(g, x) for x in a.args))
    elif a.cmd == "spans":
        print("\n\n".join(read_span(root, g, s) for s in a.args))
    elif a.cmd == "grep":
        print(grep(root, g, " ".join(a.args), a.max, a.start, a.end))
    elif a.cmd in ("recall", "find"):
        print(recall(g, " ".join(a.args)))
    elif a.cmd == "zoom":
        print(zoom(g, a.args, a.q, a.recent))
    elif a.cmd == "window":
        print(window(root, g, a.args[0], a.args[1] if len(a.args) > 1 else a.args[0]))
    elif a.cmd == "coverage":
        print(coverage(root, g, chunk=int(a.args[0]) if a.args else None, size=a.size))
    elif a.cmd == "validate":
        print(validate(root, g))


if __name__ == "__main__":
    main()
