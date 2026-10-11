#!/usr/bin/env python3
"""Build the human-facing memory atlas: one self-contained HTML page with the maps, the
session-start view the agent sees, and the raw logs (so every span opens verbatim).

Static build. For a live view that re-reads maps and logs on every page load, use remember/serve.py.

Usage: python3 remember/viz.py <out.html> [project ...] [--artifact]
  default: a standalone page you can open locally; --artifact: body-only, for publishing as a claude.ai artifact
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memmap  # noqa: E402

ROOT = memmap.ROOT


def build_data(projects=None, root=ROOT):
    projects = projects or sorted(p for p in os.listdir(os.path.join(root, "maps"))
                                  if os.path.exists(os.path.join(root, "maps", p, "graph.json")))
    data = {"projects": {}}
    for p in projects:
        g = memmap.load(root, p)
        render = memmap.render(g)
        cnt = memmap.counts(g)
        for n in g["nodes"]:
            n["_msgs"], n["_sub"] = cnt.get(n["id"], (0, 0))
        for k in ("_by_id", "_cut", "_root"):
            g.pop(k, None)
        logs = {f: [list(m) for m in memmap.log_messages(root, f)] for f in g.get("logs", [])}
        data["projects"][p] = {"graph": g, "render": render, "logs": logs}
    return data


def page(data=None, artifact=False):
    html = open(os.path.join(HERE, "viz", "template.html"), encoding="utf-8").read()
    if data is not None:
        html = html.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    if artifact:
        return html
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            '</head><body>' + html + '</body></html>')


def main(out, projects, artifact=False):
    data = build_data(projects)
    with open(out, "w", encoding="utf-8") as f:
        f.write(page(data, artifact))
    print(f"{out}: {', '.join(data['projects'])} ({os.path.getsize(out) // 1024} KB)")


if __name__ == "__main__":
    args = [a for a in sys.argv[2:] if a != "--artifact"]
    main(sys.argv[1], args, "--artifact" in sys.argv)
