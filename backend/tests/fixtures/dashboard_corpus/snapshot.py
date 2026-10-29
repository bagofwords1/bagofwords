"""Ask the API everything a person can learn about corpus conversations and
dashboards, and normalise the answers so two runs compare byte for byte.

Only reads. Left out on purpose, because calling them changes data:
exports (audit rows; PDF renders and writes a file), /runtime/* (rate-limit
rows, view counting), /r/{id}/run and /r/{id}/rerun (execute queries),
/viewed, streams, share toggles and posting to chat.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tests.fixtures.dashboard_corpus.cast import Person
from tests.fixtures.dashboard_corpus.kinds import Corpus

GOLDEN_DIR = Path(__file__).resolve().parents[2] / "golden" / "dashboards"

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
HEX32_RE = re.compile(r"\b[0-9a-f]{32}\b")
ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")
JWT_RE = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")

# Values the snapshot run itself moves, or that change on every call. Masked
# whether or not they are set: on Postgres `last_seen` is not written during
# the run, on SQLite it is.
VOLATILE_KEYS = {"last_seen", "last_login", "app_version", "token", "access_token",
                 "nonce", "view_token", "duration_ms", "version_hash"}

# Scalar lists that are sets (no order is promised). Every other list keeps
# its order: visualization_ids, preview_images, file_ids and the like are
# positional, and a reorder is a real change.
SET_KEYS = {"artifact_shared_user_ids", "artifact_shared_group_ids",
            "conversation_shared_user_ids", "conversation_shared_group_ids",
            "artifact_chat_data_source_ids", "focused_data_source_ids", "scope_ids",
            "report_ids", "artifact_modes", "permissions"}

# Collections loaded through relationships without an ORDER BY: the database
# returns them in any order (SQLite and Postgres differ). Sorted canonically.
UNORDERED_COLLECTIONS = {"dashboard_layout_versions", "widgets", "text_widgets",
                         "data_sources", "connections", "files", "artifacts"}

LIST_FILTERS = ("my", "shared", "published", "all")
LIST_VARIANTS = ({}, {"has_artifacts": "yes", "artifact_mode": "page"},
                 {"has_artifacts": "yes", "artifact_mode": "doc"})

def _sorted_dicts(items: list) -> list:
    return sorted(items, key=lambda i: json.dumps(i, sort_keys=True, ensure_ascii=False))


class Normaliser:
    """Ids become corpus names; ids the corpus did not write (rows the API
    created) become <new#n>, numbered by first appearance within one answer,
    so distinct new rows stay distinct and the same answer hashes the same."""

    def __init__(self, corpus: Corpus):
        self.names = corpus.names
        self.tokens = corpus.tokens
        self._new: dict[str, str] = {}

    def _id(self, raw: str) -> str:
        if raw in self.names:
            return f"<{self.names[raw]}>"
        if raw.lower() in self.names:  # a known id written in upper case
            return f"<{self.names[raw.lower()]}:upper>"
        if raw not in self._new:
            self._new[raw] = f"<new#{len(self._new) + 1}>"
        return self._new[raw]

    def _str(self, value: str) -> str:
        if ISO_TS_RE.match(value):
            return "<ts>"
        if value in self.tokens:
            return f"<token:{self.tokens[value]}>"
        value = JWT_RE.sub("<jwt>", value)
        value = UUID_RE.sub(lambda m: self._id(m.group(0)), value)
        return HEX32_RE.sub(lambda m: f"<token:{self.tokens[m.group(0)]}>"
                            if m.group(0) in self.tokens else "<hex>", value)

    def answer(self, body: Any) -> Any:
        self._new = {}
        return self._walk(body)

    def _walk(self, value: Any, key: str | None = None) -> Any:
        if key in VOLATILE_KEYS:
            return "<volatile>"
        if isinstance(value, dict):
            return {k: self._walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            items = [self._walk(v) for v in value]
            if key in SET_KEYS:
                return sorted(items, key=lambda i: json.dumps(i, sort_keys=True, ensure_ascii=False))
            if key in UNORDERED_COLLECTIONS:
                return _sorted_dicts(items)
            return items
        if isinstance(value, str):
            return self._str(value)
        return value


class Snapshotter:
    def __init__(self, client, corpus: Corpus):
        self.client = client
        self.corpus = corpus
        self.norm = Normaliser(corpus)
        # Each corpus row belongs to the conversation with the longest key
        # that prefixes its name ("sched_prompt_spawn/run_1/q_sales" is the
        # run's query, not its parent's).
        self._by_report: dict[str, dict[str, list[str]]] = {
            k: {"queries": [], "steps": [], "vizzes": [], "prompts": []} for k in corpus.reports}
        keys = sorted(corpus.reports, key=len, reverse=True)
        for row_id, name in sorted(corpus.names.items(), key=lambda kv: kv[1]):
            owner = next((k for k in keys if name.startswith(f"{k}/")), None)
            if owner is None:
                continue
            rest = name[len(owner) + 1:]
            if "/" not in rest and rest.startswith("q_"):
                self._by_report[owner]["queries"].append(row_id)
            elif rest.endswith("/step"):
                self._by_report[owner]["steps"].append(row_id)
            elif rest.endswith("/viz"):
                self._by_report[owner]["vizzes"].append(row_id)
            elif rest == "prompt":
                self._by_report[owner]["prompts"].append(row_id)
        self._thumbs = {p.rsplit("/", 1)[-1] for p in corpus.thumbnail_files}

    def _children(self, key: str) -> dict:
        return self._by_report[key]

    def _org(self, person: Person) -> str | None:
        if person.key == "anonymous":
            return None
        if person.key == "other_org":
            return self.corpus.cast.other_org_id
        return self.corpus.cast.org_id

    def name(self, row_id: str) -> str:
        return self.corpus.names.get(row_id, "<new>")

    def get(self, person: Person, url: str, params: dict | None = None,
            unordered: bool = False, status_only: bool = False) -> dict:
        resp = self.client.get(url, params=params, headers=person.headers(self._org(person)))
        out: dict = {"status": resp.status_code}
        if status_only:
            return out
        try:
            body = resp.json()
        except ValueError:
            body = None
        body = self.norm.answer(body)
        out["body"] = _sorted_dicts(body) if unordered and isinstance(body, list) else body
        return out

    def raw(self, person: Person, url: str) -> Any:
        resp = self.client.get(url, headers=person.headers(self._org(person)))
        return resp.json() if resp.status_code == 200 else None

    # ── per person ───────────────────────────────────────────────────

    def take(self, person: Person) -> dict:
        snap = {"lists": self.lists(person), "reports": {}}
        for key in sorted(self.corpus.reports):
            snap["reports"][key] = self.report(person, key)
        return snap

    def lists(self, person: Person) -> dict:
        """Every filter and variant records its cards in order. Cards repeat
        across filters, so bodies.json stores each distinct card once."""
        out: dict = {}
        for flt in LIST_FILTERS:
            for variant in LIST_VARIANTS:
                label = flt + "".join(f"&{k}={v}" for k, v in variant.items())
                pages, page = [], 1
                while True:
                    resp = self.client.get("/api/reports", headers=person.headers(self._org(person)),
                                           params={"filter": flt, "page": page, "limit": 100, **variant})
                    body = resp.json() if resp.status_code == 200 else None
                    if isinstance(body, dict):
                        pages.append({
                            "status": 200,
                            "meta": self.norm.answer(body.get("meta")),
                            "cards": [{"status": 200, "body": self.norm.answer(card)}
                                      for card in body.get("reports") or []],
                        })
                    else:
                        pages.append({"status": resp.status_code})
                    meta = body.get("meta") if isinstance(body, dict) else None
                    if not meta or page >= (meta.get("total_pages") or 1):
                        break
                    page += 1
                out[label] = pages
        out["projects"] = self.get(person, "/api/projects", unordered=True)
        out["notifications"] = self.get(person, "/api/notifications")
        out["scheduled_prompts_my"] = self.get(person, "/api/scheduled-prompts",
                                               params={"filter": "my"}, unordered=True)
        out["entity"] = self.get(person, f"/api/entities/{self.corpus.ids['entity_from_step']}")
        return out

    def report(self, person: Person, key: str) -> dict:
        rid = self.corpus.reports[key]
        kids = self._children(key)
        r: dict = {}

        # The public dashboard page, in the order the page itself calls.
        r["r"] = self.get(person, f"/api/r/{rid}")
        r["r_artifacts"] = self.get(person, f"/api/r/{rid}/artifacts")
        raw_rows = self.raw(person, f"/api/r/{rid}/artifacts") or []
        # What /r shows: pages/r/[id]/index.vue:743 picks the first non-doc row.
        shown = next((a for a in raw_rows if a.get("mode") != "doc"), raw_rows[0] if raw_rows else None)
        r["r_shows"] = self.name(shown["id"]) if shown else None
        r["r_artifact_detail"] = {self.name(a["id"]): self.get(person, f"/api/r/{rid}/artifacts/{a['id']}")
                                  for a in raw_rows}
        r["r_queries"] = self.get(person, f"/api/r/{rid}/queries", unordered=True)
        if shown:
            r["r_queries_shown"] = self.get(person, f"/api/r/{rid}/queries",
                                            params={"artifact_id": shown["id"]}, unordered=True)
            for f in (shown.get("content") or {}).get("files") or []:
                r.setdefault("r_embed_token", {})[self.name(f.get("id"))] = self.get(
                    person, f"/api/r/{rid}/files/{f['id']}/embed_token", status_only=True)
        r["r_steps"] = {self.name(q["id"]): self.get(person, f"/api/r/{rid}/queries/{q['id']}/step")
                        for q in self.raw(person, f"/api/r/{rid}/queries") or []}
        r["r_steps"] = dict(sorted(r["r_steps"].items()))
        r["r_chat"] = self.get(person, f"/api/r/{rid}/chat")
        r["r_chat_completions"] = self.get(person, f"/api/r/{rid}/chat/completions", params={"limit": 30})

        # The shared conversation, every page.
        token = next((t for t, k in self.corpus.tokens.items() if k == key), None)
        if token:
            pages, before = [], None
            while True:
                params = {"limit": 10, **({"before": before} if before else {})}
                resp = self.client.get(f"/api/c/{token}", params=params,
                                       headers=person.headers(self._org(person)))
                body = resp.json() if resp.status_code == 200 else None
                pages.append({"status": resp.status_code, "body": self.norm.answer(body)})
                if not body or not body.get("has_more"):
                    break
                before = body.get("next_before")
            r["c"] = pages

        # The in-app surfaces.
        r["report"] = self.get(person, f"/api/reports/{rid}")
        r["fork_status"] = self.get(person, f"/api/reports/{rid}/fork_status")
        r["shares_artifact"] = self.get(person, f"/api/reports/{rid}/shares/artifact", unordered=True)
        r["shares_conversation"] = self.get(person, f"/api/reports/{rid}/shares/conversation",
                                            unordered=True)
        r["artifact_chat_agents"] = self.get(person, f"/api/reports/{rid}/artifact_chat/agents")
        r["files"] = self.get(person, f"/api/reports/{rid}/files", unordered=True)
        r["scheduled_prompts"] = self.get(person, f"/api/reports/{rid}/scheduled-prompts",
                                          unordered=True)
        r["scheduled_prompt_runs"] = {
            self.name(sp): self.get(person, f"/api/reports/{rid}/scheduled-prompts/{sp}/runs")
            for sp in kids["prompts"]}
        r["completions"] = self.get(person, f"/api/reports/{rid}/completions", params={"limit": 50})
        r["queries"] = self.get(person, "/api/queries", params={"report_id": rid}, unordered=True)
        r["query_detail"] = {}
        for qid in kids["queries"]:
            r["query_detail"][self.name(qid)] = self.get(person, f"/api/queries/{qid}")
            r["query_detail"][self.name(qid) + "#default_step"] = self.get(
                person, f"/api/queries/{qid}/default_step")
        r["step_detail"] = {self.name(sid): self.get(person, f"/api/steps/{sid}") for sid in kids["steps"]}
        r["viz_detail"] = {self.name(vid): self.get(person, f"/api/visualizations/{vid}")
                           for vid in kids["vizzes"]}
        r["artifacts"] = self.get(person, f"/api/artifacts/report/{rid}")
        r["artifacts_latest"] = self.get(person, f"/api/artifacts/report/{rid}/latest")
        detail = {}
        for a in raw_rows:
            name = self.name(a["id"])
            detail[name] = self.get(person, f"/api/artifacts/{a['id']}")
            if a.get("mode") == "slides":
                detail[name + "#previews"] = self.get(person, f"/api/artifacts/{a['id']}/previews")
                detail[name + "#preview_0"] = self.get(person, f"/api/artifacts/{a['id']}/preview/0",
                                                       status_only=True)
        r["artifact_detail"] = detail
        thumbs = {}
        for vid, name in self.corpus.names.items():
            if name.startswith(f"{key}/") and f"{vid}.png" in self._thumbs:
                thumbs[name] = self.get(person, f"/api/thumbnails/{vid}.png", status_only=True)
        r["thumbnails"] = dict(sorted(thumbs.items()))
        return r


# ── golden files ─────────────────────────────────────────────────────
#
# Most answers are identical across readers (every viewer of a public
# dashboard gets the same /r payload), so each distinct body is stored once
# in bodies.json under a content hash, and a reader's file holds the status
# and the hash. A changed body shows up as a changed hash, and the test
# prints the body diff behind it.

BODIES_PATH = GOLDEN_DIR / "bodies.json"


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True)


def body_hash(body: Any) -> str:
    import hashlib

    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()[:16]


def split_bodies(snapshot: Any, bodies: dict) -> Any:
    """Replace every {"status", "body"} answer's body with its hash."""
    if isinstance(snapshot, dict):
        if "status" in snapshot and "body" in snapshot:
            h = body_hash(snapshot["body"])
            bodies[h] = snapshot["body"]
            return {**{k: v for k, v in snapshot.items() if k != "body"}, "body": f"#{h}"}
        return {k: split_bodies(v, bodies) for k, v in snapshot.items()}
    if isinstance(snapshot, list):
        return [split_bodies(v, bodies) for v in snapshot]
    return snapshot


def dump(snapshot: dict) -> str:
    return canonical(snapshot) + "\n"


def golden_path(person_key: str) -> Path:
    return GOLDEN_DIR / f"{person_key}.json"


def load_bodies() -> dict:
    if not BODIES_PATH.exists():
        return {}
    return json.loads(BODIES_PATH.read_text(encoding="utf-8"))


def write_bodies(bodies: dict) -> None:
    BODIES_PATH.write_text(canonical(dict(sorted(bodies.items()))) + "\n", encoding="utf-8")


def referenced_hashes(snapshot: Any, out: set) -> set:
    if isinstance(snapshot, dict):
        for v in snapshot.values():
            referenced_hashes(v, out)
    elif isinstance(snapshot, list):
        for v in snapshot:
            referenced_hashes(v, out)
    elif isinstance(snapshot, str) and snapshot.startswith("#") and len(snapshot) == 17:
        out.add(snapshot[1:])
    return out


def explain_diff(role: str, expected: str, actual: str, old_bodies: dict, new_bodies: dict,
                 limit: int = 120) -> str:
    """A unified diff of the reader file, plus the body diff behind each
    changed hash."""
    import difflib

    lines = list(difflib.unified_diff(expected.splitlines(), actual.splitlines(),
                                      f"golden/{role}", f"actual/{role}", lineterm="", n=3))
    out = lines[:limit]
    # A changed answer is one "-" hash line followed by one "+" hash line, in
    # the same order on both sides.
    removed = [ln.split('"#')[1][:16] for ln in lines if ln.startswith("-") and '"#' in ln]
    added = [ln.split('"#')[1][:16] for ln in lines if ln.startswith("+") and '"#' in ln]
    for old, new in zip(removed, added, strict=False):
        a = canonical(old_bodies.get(old)).splitlines()
        b = canonical(new_bodies.get(new)).splitlines()
        out.append(f"--- body #{old} -> #{new}")
        out.extend(list(difflib.unified_diff(a, b, lineterm="", n=2))[2:60])
    return "\n".join(out)


def summary_rows(snapshots: dict[str, dict], *, partial: bool = False) -> str:
    """SUMMARY.md: one line per conversation, one column per person.

    A cell reads `<status of /api/r/{id}> <what /r shows>` -- the facts the
    later phases are most likely to change, in a form a person can check
    against the code by eye.
    """
    people = list(snapshots)
    reports = sorted(next(iter(snapshots.values()))["reports"])
    lines = ["| conversation | " + " | ".join(people) + " |",
             "|---|" + "---|" * len(people)]
    for key in reports:
        cells = []
        for p in people:
            # While BOW_UPDATE_GOLDEN rewrites one group at a time (partial),
            # the other groups' files may not know a newly seeded conversation
            # yet; anywhere else a missing conversation is an error.
            rep = snapshots[p]["reports"].get(key) if partial else snapshots[p]["reports"][key]
            if rep is None:
                cells.append("—")
                continue
            st = rep["r"]["status"]
            cells.append(f"{st} {rep['r_shows']}" if st == 200 and rep["r_shows"] else str(st))
        lines.append(f"| {key} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"
