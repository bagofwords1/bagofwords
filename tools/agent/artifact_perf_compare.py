#!/usr/bin/env python3
"""Compare two artifact_perf_harness.mjs runs (before vs after a change).

    python3 tools/agent/artifact_perf_compare.py out/before out/after [--md report.md]

Behavior must be identical — any difference in outcome, rendered text,
per-testid text/computed style, element counts, interaction results,
ARTIFACT_* messages, host banners or failing responses is listed as a
REGRESSION. Screenshots are pixel-diffed (Pillow). Timings are summarized
(median per surface × cache state) along with bytes the browser downloaded
for /libs.
"""
import argparse
import json
import re
import statistics
import sys
from pathlib import Path

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
NUXT_CHUNK = re.compile(r"/_nuxt/[^\s:)]+")


def key(r):
    return (r["name"], r["surface"], r["role"], r["cache"])


def norm_errors(lines):
    out = []
    for l in lines or []:
        l = UUID.sub("<id>", l)
        l = NUXT_CHUNK.sub("/_nuxt/<chunk>", l)
        l = re.sub(r"\?h=[0-9a-f]+", "", l)
        out.append(l)
    return sorted(out)


def behavior(r):
    fp = r.get("fingerprint") or {}
    errs = [m.get("payload", {}).get("payload", {}).get("message") for m in r.get("messages", []) if m["type"] == "ARTIFACT_ERROR"]
    return {
        "outcome": r["outcome"],
        "text": fp.get("text"),
        "testids": fp.get("testids"),
        "styles": fp.get("styles"),
        "counts": fp.get("counts"),
        "globals": fp.get("globals"),
        "artifact_messages": sorted({m["type"] for m in r.get("messages", [])} - {"ARTIFACT_VIEW_TOKEN_REQUEST"}),
        "artifact_errors": errs,
        "interactions": r.get("interactions"),
        "host": r.get("hostText"),
        "bad_responses": norm_errors(r.get("badResponses")),
    }


def pixel_diff(a: Path, b: Path):
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return None
    if not a.exists() or not b.exists():
        return None
    ia, ib = Image.open(a).convert("RGB"), Image.open(b).convert("RGB")
    if ia.size != ib.size:
        return f"size {ia.size} vs {ib.size}"
    diff = ImageChops.difference(ia, ib).convert("L").point(lambda v: 255 if v > 24 else 0)
    changed = sum(1 for v in diff.tobytes() if v)
    return changed / (ia.size[0] * ia.size[1])


def lib_bytes(r):
    return sum(q["bytes"] for q in r.get("requests", []) if "/libs/" in q["url"] and (q.get("bytes") or 0) > 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--md")
    ap.add_argument("--ignore", default="", help="comma list of behavior fields to ignore")
    a = ap.parse_args()
    ignore = set(filter(None, a.ignore.split(",")))
    B = {key(r): r for r in json.load(open(Path(a.before) / "results.json"))}
    A = {key(r): r for r in json.load(open(Path(a.after) / "results.json"))}

    lines = []
    regressions = 0
    lines.append("## Behavior (before vs after)\n")
    lines.append("| case | outcome | behavior | screenshot Δ |")
    lines.append("|---|---|---|---|")
    for k in sorted(set(B) | set(A)):
        if k not in B or k not in A:
            lines.append(f"| {' '.join(k)} | missing in {'after' if k not in A else 'before'} | ❌ | |")
            regressions += 1
            continue
        bb, aa = behavior(B[k]), behavior(A[k])
        diffs = [f for f in bb if f not in ignore and bb[f] != aa[f]]
        improved = []
        # The one sanctioned difference: an error the browser used to mask as
        # "Script error." (thrown inside a cross-origin /libs script) now
        # reports its real message. Same failure, same outcome — only the
        # text the error UI / fix loop receives got better.
        if "artifact_errors" in diffs and bb["artifact_errors"] == ["Script error."] \
                and aa["artifact_errors"] and aa["artifact_errors"] != ["Script error."] \
                and bb["outcome"] == aa["outcome"] == "error":
            diffs.remove("artifact_errors")
            improved.append(f"error message unmasked: {aa['artifact_errors'][0][:80]!r}")
        px = ""
        if k[3] == "cold":
            shot = f"{k[0]}__{k[1]}__{k[2]}.png"
            d = pixel_diff(Path(a.before) / "shots" / shot, Path(a.after) / "shots" / shot)
            px = "n/a" if d is None else (d if isinstance(d, str) else f"{d * 100:.2f}%")
        status = ("✅ identical" if not improved else "✅ same behavior; " + "; ".join(improved)) if not diffs else "❌ " + ", ".join(diffs)
        if diffs:
            regressions += 1
        lines.append(f"| {' '.join(k)} | {bb['outcome']} → {aa['outcome']} | {status} | {px} |")
        for f in diffs:
            lines.append(f"|  | `{f}` before | `{json.dumps(bb[f], ensure_ascii=False)[:300]}` | |")
            lines.append(f"|  | `{f}` after | `{json.dumps(aa[f], ensure_ascii=False)[:300]}` | |")

    lines.append("\n## Timing (median ms from navigation start)\n")
    lines.append("| surface | cache | n | paint before | paint after | ready before | ready after | /libs bytes before | /libs bytes after |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    groups = {}
    for k in B:
        if k in A and B[k]["outcome"] in ("ready", "painted_no_signal") and A[k]["outcome"] == B[k]["outcome"]:
            groups.setdefault((k[1], k[3]), []).append(k)
    for (surface, cache), ks in sorted(groups.items()):
        def med(src, field):
            vals = [src[k][field] for k in ks if src[k].get(field) is not None]
            return round(statistics.median(vals)) if vals else None
        def medb(src):
            return round(statistics.median([lib_bytes(src[k]) for k in ks]))
        lines.append(f"| {surface} | {cache} | {len(ks)} | {med(B, 'firstPaintMs')} | {med(A, 'firstPaintMs')} | "
                     f"{med(B, 'readyMs')} | {med(A, 'readyMs')} | {medb(B):,} | {medb(A):,} |")

    lines.append("\n## Per-case timing (ms)\n")
    lines.append("| case | paint before | paint after | ready before | ready after |")
    lines.append("|---|---|---|---|---|")
    for k in sorted(B):
        if k in A:
            fb, fa = B[k].get("firstPaintMs"), A[k].get("firstPaintMs")
            rb, ra = B[k].get("readyMs"), A[k].get("readyMs")
            r = lambda v: "—" if v is None else round(v)
            lines.append(f"| {' '.join(k)} | {r(fb)} | {r(fa)} | {r(rb)} | {r(ra)} |")

    out = "\n".join(lines)
    print(out)
    print(f"\nREGRESSIONS: {regressions}")
    if a.md:
        Path(a.md).write_text(out + f"\n\nREGRESSIONS: {regressions}\n")
    sys.exit(1 if regressions else 0)


if __name__ == "__main__":
    main()
