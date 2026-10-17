"""Build the hosted Life Wiki Dashboard.

Pulls the Life Wiki from the Notion API, adds the Skill Tree export, and writes
site/ = index.html (web-src/ shell + dashboard page) + data.enc (AES-GCM, key from DASHBOARD_PASSWORD via
PBKDF2-SHA256), the same browser format as the Garmin dashboard.

Environment:
  NOTION_TOKEN        internal integration secret, connected to the Life Wiki page
  DASHBOARD_PASSWORD  password the page asks for
  SKILLTREE_GIST      id of the private gist holding skilltree.json (the page saves unlocks there)
  SKILLTREE_JSON      fallback: the Skill Tree app's export (JSON text)
  SKILLTREE_FILE      or a path to that export, for local builds

Usage:
  python build.py site            build from Notion
  python build.py site --sample   build from sample_data.json (no Notion call)
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ROOT = Path(__file__).resolve().parent
NOTION_VERSION = "2025-09-03"
PBKDF2_ITERATIONS = 600_000

# The Life Wiki page and its databases: (name, database id, data source id).
LIFE_WIKI_PAGE = "e52fc193f0954ba88c24c996b20e6182"
PROJECTS = ("Active Projects", "99ac0a23807847ef911c4647ec92f2da", "d7e2eda4-a2e7-4373-80b0-c09440a11edb")
GOALS = ("Impossible List", "b4cc061661b3469ab576f5e2facb9693", "3cdebe34-6799-462f-82c9-a1507dca1647")
TASKS = ("Task Planner", "803e0b92530f4beea2f0a5c012bda99b", "7dcac3d2-944f-482a-9394-85a9e2e8b7a2")


class NotionError(Exception):
    def __init__(self, code: int, path: str, detail: str):
        super().__init__(f"Notion API error {code} on {path}: {detail}")
        self.code = code


# ---------------------------------------------------------------- Notion API
def notion(method: str, path: str, body: dict | None = None) -> dict:
    token = os.environ.get("NOTION_TOKEN", "").strip()
    if not token:
        sys.exit("NOTION_TOKEN is not set. Add it as a repository secret (see README).")
    req = urllib.request.Request(
        "https://api.notion.com/v1" + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": "Bearer " + token, "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"},
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return json.load(res)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(float(e.headers.get("Retry-After") or 2 ** attempt))
                continue
            if e.code == 401:
                sys.exit(f"Notion refused the token (401). Check NOTION_TOKEN. {detail}")
            raise NotionError(e.code, path, detail) from None
    raise RuntimeError("unreachable")


def page_url(page_id: str) -> str:
    return "https://app.notion.com/p/" + page_id.replace("-", "")


def plain(rich: list) -> str:
    return "".join(r.get("plain_text", "") for r in rich or [])


def prop_columns(name: str, prop: dict) -> dict:
    """One Notion property -> the columns the dashboard reads (same shape as the Notion connector's rows)."""
    kind = prop.get("type")
    v = prop.get(kind)
    if kind == "title":
        return {name: plain(v)}
    if kind == "rich_text":
        return {name: plain(v).replace("\n", "<br>")}
    if kind in ("select", "status"):
        return {name: (v or {}).get("name")} if v else {}
    if kind == "multi_select":
        return {name: json.dumps([o["name"] for o in v or []], ensure_ascii=False)}
    if kind == "relation":
        return {name: json.dumps([page_url(r["id"]) for r in v or []])}
    if kind == "checkbox":
        return {name: "__YES__" if v else "__NO__"}
    if kind == "date":
        if not v:
            return {f"date:{name}:is_datetime": 0}
        start, end = v.get("start") or "", v.get("end") or ""
        out = {f"date:{name}:start": start, f"date:{name}:is_datetime": int("T" in start)}
        if end:
            out[f"date:{name}:end"] = end
        return out
    if kind in ("number", "url", "email", "phone_number"):
        return {name: v}
    if kind == "formula":
        return {name: (v or {}).get((v or {}).get("type"))}
    return {}


def query_rows(source: tuple[str, str, str]) -> list[dict]:
    rows, cursor = [], None
    while True:
        body = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
        res = notion("POST", f"/data_sources/{source[2]}/query", body)
        for page in res["results"]:
            row = {"url": page_url(page["id"])}
            if page.get("object") != "page":
                continue
            for name, prop in page["properties"].items():
                row.update(prop_columns(name, prop))
            rows.append(row)
        if not res.get("has_more"):
            return rows
        cursor = res["next_cursor"]


def block_children(block_id: str) -> list[dict]:
    out, cursor = [], None
    while True:
        res = notion("GET", f"/blocks/{block_id}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else ""))
        out += res["results"]
        if not res.get("has_more"):
            return out
        cursor = res["next_cursor"]


def page_text(page_id: str) -> str:
    """Flatten a page into the lines the dashboard's principles reader expects."""
    lines: list[str] = []

    def walk(blocks: list[dict]) -> None:
        number = 0
        for b in blocks:
            t = b["type"]
            body = b.get(t, {})
            number = number + 1 if t == "numbered_list_item" else 0
            if t in ("heading_1", "heading_2", "heading_3"):
                lines.append("#" * int(t[-1]) + " " + plain(body.get("rich_text")))
            elif t == "numbered_list_item":
                lines.append(f"{number}. " + plain(body.get("rich_text")))
            elif t == "divider":
                lines.append("---")
            elif "rich_text" in body:
                lines.append(plain(body.get("rich_text")))
            if t == "column_list":
                walk(block_children(b["id"]))
            elif t == "column":
                walk(block_children(b["id"]))
                lines.append("</column>")
            elif b.get("has_children") and t not in ("child_page", "child_database"):
                walk(block_children(b["id"]))

    walk(block_children(page_id))
    return "\n".join(lines)


def diagnose() -> None:
    """Say what the integration can see. Counts and yes/no only: the Actions log of a public repo is public."""
    def visible(path: str) -> str:
        try:
            notion("GET", path)
            return "visible"
        except NotionError as e:
            return f"NOT visible ({e.code})"
    print("Notion access check:")
    print("  Life Wiki page:", visible(f"/pages/{LIFE_WIKI_PAGE}"))
    for name, db, ds in (PROJECTS, GOALS, TASKS):
        print(f"  {name}: database {visible(f'/databases/{db}')}, data source {visible(f'/data_sources/{ds}')}")
    try:
        res = notion("POST", "/search", {"page_size": 100})
        kinds: dict[str, int] = {}
        for r in res.get("results", []):
            kinds[r.get("object", "?")] = kinds.get(r.get("object", "?"), 0) + 1
        print("  Shared with the integration (first 100):", kinds or "nothing")
    except NotionError as e:
        print("  Search failed:", e.code)


# ---------------------------------------------------------------- Skill Tree
def read_gist(gist_id: str) -> dict | None:
    # A secret gist is readable by id without signing in (the Actions GITHUB_TOKEN gets 403 on gists).
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "life-dashboard-build"}
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://api.github.com/gists/{gist_id}", headers=headers), timeout=30) as res:
            g = json.load(res)
        f = (g.get("files") or {}).get("skilltree.json")
        if not f:
            return None
        raw = f["content"]
        if f.get("truncated") and f.get("raw_url"):
            with urllib.request.urlopen(urllib.request.Request(f["raw_url"], headers={"User-Agent": "life-dashboard-build"}), timeout=30) as res:
                raw = res.read().decode()
        return json.loads(raw)
    except (urllib.error.URLError, ValueError) as e:
        print(f"Could not read the skill tree gist ({e}); falling back to the export")
        return None


def load_skill_export() -> dict | None:
    gist_id = os.environ.get("SKILLTREE_GIST", "").strip()
    if gist_id:
        data = read_gist(gist_id)
        if data:
            return data
    raw = os.environ.get("SKILLTREE_JSON", "").strip()
    path = os.environ.get("SKILLTREE_FILE", "").strip()
    if not raw and path:
        raw = Path(path).read_text(encoding="utf-8")
    return json.loads(raw) if raw else None


# ---------------------------------------------------------------- encryption
def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def encrypt_for_browser(payload: dict, password: str, salt: bytes) -> dict:
    iv = os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS, dklen=32)
    plain_bytes = gzip.compress(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": PBKDF2_ITERATIONS, "salt": _b64(salt),
            "iv": _b64(iv), "ct": _b64(AESGCM(key).encrypt(iv, plain_bytes, None))}


def decrypt_from_browser_format(blob: dict, password: str) -> dict:
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(blob["salt"]), blob["iter"], dklen=32)
    return json.loads(gzip.decompress(AESGCM(key).decrypt(base64.b64decode(blob["iv"]), base64.b64decode(blob["ct"]), None)))


def deployment_salt() -> bytes:
    """Stable per-site salt (not secret) so 'Remember me' survives rebuilds."""
    return base64.b64decode((ROOT / "salt.txt").read_text().strip())


# ---------------------------------------------------------------- build
def collect(sample: bool) -> dict:
    if sample:
        payload = json.loads((ROOT / "sample_data.json").read_text(encoding="utf-8"))
        payload["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return payload
    export = load_skill_export()
    try:
        sources = {
            "projects": query_rows(PROJECTS),
            "goals": query_rows(GOALS),
            "tasks": query_rows(TASKS),
            "wiki": {"text": page_text(LIFE_WIKI_PAGE)},
        }
    except NotionError as e:
        print(e)
        diagnose()
        sys.exit("Notion can't see part of the Life Wiki. In Notion open Life Wiki -> ••• -> Connections and add the integration.")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": sources,
        "skilltree": {"config": export["config"], "state": export.get("state") or {}} if export else None,
        "skill_gist": os.environ.get("SKILLTREE_GIST", "").strip() or None,
    }


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = Path(args[0] if args else "site")
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    if not password:
        sys.exit("DASHBOARD_PASSWORD is not set. Add it as a repository secret (see README).")
    payload = collect("--sample" in sys.argv)
    s = payload["sources"]
    tree = payload.get("skilltree") or {}
    skills = sum(len(t["nodes"]) for b in (tree.get("config") or {}).get("branches", []) for t in b["tracks"])
    print(f"projects={len(s['projects'])} goals={len(s['goals'])} tasks={len(s['tasks'])} "
          f"wiki_chars={len(s['wiki']['text'])} skills={skills} gist={'yes' if payload.get('skill_gist') else 'no'}")
    blob = encrypt_for_browser(payload, password, deployment_salt())
    assert decrypt_from_browser_format(blob, password)["generated_at"] == payload["generated_at"]
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    src = ROOT / "web-src"
    page = "".join((src / f).read_text(encoding="utf-8") for f in ("shell-head.html", "dashboard.html", "skills.html", "shell-tail.html"))
    (out / "index.html").write_text(page, encoding="utf-8")
    (out / "data.enc").write_text(json.dumps(blob), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    print(f"wrote {out / 'data.enc'}")


if __name__ == "__main__":
    main()
