"""Shared DSL envelope and DataFrame contract for Elasticsearch and OpenSearch."""
import json

import pandas as pd

from app.data_sources.clients.elasticsearch_guardrails import QueryGuardrailError


SEARCH_KEYS = frozenset({
    "query", "aggs", "aggregations", "size", "sort", "_source", "from",
    "search_after", "timeout", "post_filter", "collapse", "fields", "track_total_hits",
    "docvalue_fields", "stored_fields", "min_score",
})

SEARCH_GUIDE = """
Search DSL: use {"index": "...", "body": {...}} or put search keys beside index.
For an exact document count use size: 0 and track_total_hits: true with your query.
Read df.iloc[0]["doc_count"]; relation "eq" means exact and "gte" means at least.
len(df) counts the page rows only. For a filtered group use a filter aggregation;
the returned columns are <name>.doc_count and <name>.<metric>. A top-level
scalar metric is the column named by its aggregation. To get the overall count
alongside single-row metric or filter aggregations, request track_total_hits: true; the table also
contains doc_count and relation when those names are available. For bucket tables,
each doc_count belongs to its bucket. With track_total_hits: true, read the overall
count as df.attrs["total_hits"]["value"] and check df.attrs["total_hits"]["relation"].
Example: aggs={"peak": {"max": {"field": "latency"}}} produces df["peak"].
For a filter "subset" containing that metric, read df["subset.peak"]. Inspect df.columns before transforming results. A missing
column is an error; never substitute zero for a missing count.
Use post_filter for document hits only; put a filter in query to scope aggregations too.
Requested fields are fields.<name> arrays, and _sort holds search_after cursor values.
An explicit positive size with aggregations returns one envelope row with raw hits and
aggregations. Use size: 0 for a flattened aggregation table.
"""


def reject_unknown(mapping, allowed, label):
    unknown = sorted(mapping.keys() - allowed)
    if unknown:
        raise ValueError(f"Unsupported {label}: {', '.join(unknown)}")


def search_body(envelope, supported):
    if "body" in envelope:
        reject_unknown(envelope, {"index", "body"}, "keys alongside body")
        body = envelope["body"]
        if not isinstance(body, dict):
            raise ValueError("Search body must be an object")
        body = body.copy()
    else:
        body = {k: v for k, v in envelope.items() if k != "index"}
    reject_unknown(body, supported, "search options")
    if isinstance(body.get("collapse"), dict):
        # Expanding inner_hits launches extra searches outside the page budget.
        reject_unknown(body["collapse"], {"field"}, "collapse options")
    for key in ("size", "from"):
        if key in body and (type(body[key]) is not int or body[key] < 0):
            raise ValueError(f"{key} must be a non-negative integer")
    if body.get("size") == 0 and not ({"aggs", "aggregations"} & body.keys()):
        if body.setdefault("track_total_hits", True) is False:
            raise ValueError("A count-only search needs track_total_hits")
    return body


def search_frame(result, body, *, include_index):
    if result.get("timed_out") or result.get("terminated_early") or (result.get("_shards") or {}).get("failed"):
        raise QueryGuardrailError("Search returned partial results (timeout, early termination or failed shards).")
    hits = result.get("hits", {})
    total = hits.get("total")
    docs = hits.get("hits", [])
    if "aggs" in body or "aggregations" in body:
        aggs = result["aggregations"]
        if body["size"] > 0:
            frame = pd.DataFrame([{"hits": docs, "aggregations": aggs, "total": total}])
        else:
            frame = pd.DataFrame(flatten_aggregations(aggs))
            frame.attrs["aggregations"] = aggs
            if (
                body.get("track_total_hits") is True
                and isinstance(total, dict)
                and len(frame) == 1
                and not {"doc_count", "relation"}.intersection(frame.columns)
                and all(isinstance(agg, dict) and "buckets" not in agg for agg in aggs.values())
            ):
                frame["doc_count"] = total["value"]
                frame["relation"] = total["relation"]
    elif body["size"] == 0:
        if not isinstance(total, dict) or total.get("relation") not in ("eq", "gte"):
            raise ValueError("Search returned no usable hit count")
        frame = pd.DataFrame([{"doc_count": total["value"], "relation": total["relation"]}])
    else:
        frame = pd.json_normalize([hit.get("_source") or {} for hit in docs], sep=".")
        if docs:
            frame.insert(0, "_id", [hit.get("_id") for hit in docs])
            if include_index:
                frame.insert(1, "_index", [hit.get("_index") for hit in docs])
            if any("sort" in hit for hit in docs):
                frame.insert(len(frame.columns), "_sort", [hit.get("sort") for hit in docs])
            for key in sorted({key for hit in docs for key in hit.get("fields", {})}):
                frame.insert(len(frame.columns), f"fields.{key}", [hit.get("fields", {}).get(key) for hit in docs])
    if total is not None:
        frame.attrs["total_hits"] = total
    return frame


def metric_columns(name, body):
    if not isinstance(body, dict):
        return {name: body}
    if "value" in body and set(body) <= {"value", "value_as_string"}:
        return {name: body.get("value_as_string", body["value"])}
    if set(body) == {"values"} and isinstance(body["values"], dict):
        return {f"{name}.{key}": value for key, value in body["values"].items()}
    if any(not isinstance(value, (list, dict)) for value in body.values()):
        columns = {}
        for key, value in body.items():
            columns.update(metric_columns(f"{name}.{key}", value))
        return columns
    return {name: json.dumps(body)}


def flatten_aggregations(aggregations):
    """Keep sibling bucket series separate; broadcast scalar metrics over their rows."""
    rows = []
    metrics = {}
    for name, body in aggregations.items():
        if isinstance(body, dict) and "doc_count" in body and "buckets" not in body:
            nested = {key: value for key, value in body.items() if isinstance(value, dict)}
            children = flatten_aggregations(nested)
            parent = {f"{name}.{key}": value for key, value in body.items() if key not in nested}
            qualified = []
            for child in children:
                columns = {(key if key == "doc_count" else f"{name}.{key}"): value
                           for key, value in child.items()}
                qualified.append({**parent, **columns})
            if len(qualified) > 1:
                rows.extend(qualified)
            else:
                metrics.update(qualified[0] if qualified else parent)
            continue
        if not isinstance(body, dict) or "buckets" not in body:
            metrics.update(metric_columns(name, body))
            continue
        buckets = body["buckets"]
        if isinstance(buckets, dict):
            buckets = [dict(bucket, key=key) for key, bucket in buckets.items()]
        for bucket in buckets:
            base = {name: bucket.get("key_as_string", bucket.get("key")), "doc_count": bucket.get("doc_count")}
            children = {k: v for k, v in bucket.items() if isinstance(v, dict) and k != "key"}
            for child in flatten_aggregations(children) or [{}]:
                # Retain the parent's count when the innermost bucket supplies doc_count.
                if "doc_count" in child:
                    base[f"{name}.doc_count"] = base["doc_count"]
                rows.append({**base, **child})
    if rows:
        return [{**metrics, **row} for row in rows]
    return [metrics] if metrics else []
