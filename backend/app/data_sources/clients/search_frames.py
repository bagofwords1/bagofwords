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
In both inspections and saved tables, read row = df.iloc[0], then row["doc_count"]
and row["relation"]. Report an exact count only for "eq"; "gte" means at least.
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
Aggregation searches default to a flat table. Zero bucket rows can coexist with matching
documents. Use track_total_hits: true and df.attrs["total_hits"] for the document count.
To inspect the original search response, add "result_format": "raw" next to "index"
(outside "body") and read df.iloc[0]["response"].
"""


def reject_unknown(mapping, allowed, label):
    unknown = sorted(mapping.keys() - allowed)
    if unknown:
        raise ValueError(f"Unsupported {label}: {', '.join(unknown)}")


def search_body(envelope, supported):
    result_format = envelope.get("result_format", "table")
    if result_format not in ("table", "raw"):
        raise ValueError("result_format must be table or raw")
    if "body" in envelope:
        reject_unknown(envelope, {"index", "body", "result_format"}, "keys alongside body")
        body = envelope["body"]
        if not isinstance(body, dict):
            raise ValueError("Search body must be an object")
        body = body.copy()
    else:
        body = {k: v for k, v in envelope.items() if k not in ("index", "result_format")}
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
    return body, result_format


def search_frame(result, body, *, include_index, opensearch=False, result_format="table"):
    # OpenSearch flags collector optimization on complete aggregation-only searches.
    has_aggs = "aggs" in body or "aggregations" in body
    optimized = opensearch and body["size"] == 0 and has_aggs and not body.get("terminate_after")
    if result.get("timed_out") or (result.get("terminated_early") and not optimized) or (result.get("_shards") or {}).get("failed"):
        raise QueryGuardrailError("Search returned partial results (timeout, early termination or failed shards).")
    if result_format == "raw":
        return pd.DataFrame([{"response": result}])
    hits = result.get("hits", {})
    total = hits.get("total")
    docs = hits.get("hits", [])
    if type(total) is int and total >= 0:
        # An integer has no precision marker. Exact tracking is guaranteed only
        # for an explicit exact request (also the default for count-only reads).
        total = {"value": total, "relation": "eq" if body.get("track_total_hits") is True else "gte"}
    if has_aggs:
        aggs = result.get("aggregations") or {}
        specs = body.get("aggs", body.get("aggregations", {}))
        rows, columns, grouped = flatten_aggregations(aggs, specs)
        frame = pd.DataFrame(rows)
        if not columns.keys() <= set(frame.columns):
            # Keep the existing order of populated columns for saved reports.
            frame = frame.reindex(columns=list(dict.fromkeys([*frame.columns, *columns])))
        if (
            body.get("track_total_hits") is True
            and isinstance(total, dict)
            and len(frame) == 1
            and not {"doc_count", "relation"}.intersection(frame.columns)
            and not grouped
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
            # Reserve metadata names while preserving every colliding source field.
            reserved = {"_id"}
            if include_index:
                reserved.add("_index")
            if any("sort" in hit for hit in docs):
                reserved.add("_sort")
            reserved.update(f"fields.{key}" for hit in docs for key in hit.get("fields", {}))
            for name in sorted(reserved.intersection(frame.columns)):
                alias = f"_source.{name}"
                while alias in frame.columns or alias in reserved:
                    alias = f"_source.{alias}"
                frame.rename(columns={name: alias}, inplace=True)
            frame.insert(0, "_id", [hit.get("_id") for hit in docs])
            if include_index:
                frame.insert(1, "_index", [hit.get("_index") for hit in docs])
            if any("sort" in hit for hit in docs):
                frame.insert(len(frame.columns), "_sort", [hit.get("sort") for hit in docs])
            for key in sorted({key for hit in docs for key in hit.get("fields", {})}):
                frame.insert(len(frame.columns), f"fields.{key}", [hit.get("fields", {}).get(key) for hit in docs])
    if body["size"] == 0 or has_aggs:
        frame.attrs["bow_search_summary"] = list(frame.columns)
        # The executor includes query notices in captured stdout, including on
        # failed runs. Give the retry the actual schema without printing data.
        columns = ", ".join(str(name) for name in frame.columns[:20])[:500]
        notice = f"Search summary columns: {columns or '(none)'}. "
        if not has_aggs:
            notice += "Read row['doc_count'] and row['relation'] for the count. "
        if isinstance(total, dict):
            notice += "total_hits metadata is a dict with value and relation."
        frame.attrs["bow_notice"] = notice.strip()
    if total is not None:
        frame.attrs["total_hits"] = total
    return frame


def metric_columns(name, body):
    if not isinstance(body, dict):
        return {name: body}
    if "value" in body and set(body) <= {"value", "value_as_string", "meta"}:
        return {name: body.get("value_as_string", body["value"])}
    if set(body) == {"values"} and isinstance(body["values"], dict):
        return {f"{name}.{key}": value for key, value in body["values"].items()}
    if any(not isinstance(value, (list, dict)) for value in body.values()):
        columns = {}
        for key, value in body.items():
            columns.update(metric_columns(f"{name}.{key}", value))
        return columns
    return {name: json.dumps(body)}


# For absent buckets the engine supplies no example of a child aggregation.
# Derive only names known from the DSL; dynamic response keys have no fixed schema.
_SINGLE_BUCKET = {"filter", "global", "nested", "reverse_nested", "missing",
                  "sampler", "diversified_sampler", "children", "parent"}
_MULTI_BUCKET = {"terms", "multi_terms", "filters", "range", "date_range", "ip_range",
                 "histogram", "date_histogram", "auto_date_histogram", "composite",
                 "significant_terms", "significant_text", "rare_terms", "adjacency_matrix",
                 "geohash_grid", "geotile_grid", "geohex_grid", "geo_distance"}


def _declared_layout(specs):
    """Model the flat layout for a request with no response children."""
    columns = {}
    grouped = False
    if not isinstance(specs, dict):
        return columns, grouped
    for name, spec in specs.items():
        if not isinstance(spec, dict):
            continue
        kinds = spec.keys()
        child_specs = spec.get("aggs", spec.get("aggregations", {}))
        children, child_groups = _declared_layout(child_specs) if child_specs else ({}, False)
        if kinds & _MULTI_BUCKET:
            grouped = True
            columns.update(dict.fromkeys([name, "doc_count", *children]))
            if child_groups:
                columns[f"{name}.doc_count"] = None
        elif kinds & _SINGLE_BUCKET:
            grouped |= child_groups
            columns[f"{name}.doc_count"] = None
            columns.update(dict.fromkeys(_single_bucket_key(name, key) for key in children))
        elif "stats" in spec or "extended_stats" in spec:
            columns.update(dict.fromkeys(f"{name}.{key}" for key in ("count", "min", "max", "avg", "sum")))
        elif kinds & {"avg", "min", "max", "sum", "value_count", "cardinality", "weighted_avg",
                      "bucket_script", "avg_bucket", "sum_bucket", "cumulative_sum", "serial_diff"}:
            columns[name] = None
    return columns, grouped


def _single_bucket_key(name, key):
    return key if key == "doc_count" else f"{name}.{key}"


def flatten_aggregations(aggregations, specs):
    """Return rows, ordered columns and whether rows represent buckets."""
    if not aggregations:
        columns, grouped = _declared_layout(specs)
        return [], columns, grouped
    rows = []
    columns = {}
    metrics = {}
    grouped = False
    for name, body in aggregations.items():
        spec = specs.get(name) if isinstance(specs, dict) else None
        children_spec = spec.get("aggs", spec.get("aggregations", {})) if isinstance(spec, dict) else {}
        if isinstance(body, dict) and "doc_count" in body and "buckets" not in body:
            nested = {key: value for key, value in body.items() if isinstance(value, dict)}
            children, child_columns, child_groups = flatten_aggregations(nested, children_spec)
            parent = {f"{name}.{key}": value for key, value in body.items() if key not in nested}
            columns.update(dict.fromkeys(parent))
            columns.update(dict.fromkeys(_single_bucket_key(name, key) for key in child_columns))
            qualified = [{**parent, **{_single_bucket_key(name, key): v for key, v in child.items()}}
                         for child in children]
            if child_groups:
                grouped = True
                rows.extend(qualified)
            else:
                metrics.update(qualified[0] if qualified else parent)
            continue
        if not isinstance(body, dict) or "buckets" not in body:
            values = metric_columns(name, body)
            metrics.update(values)
            columns.update(dict.fromkeys(values))
            continue
        grouped = True
        columns.update(dict.fromkeys((name, "doc_count")))
        buckets = body["buckets"]
        if not buckets:
            child_columns, child_groups = _declared_layout(children_spec)
            columns.update(child_columns)
            if child_groups:
                columns[f"{name}.doc_count"] = None
        if isinstance(buckets, dict):
            buckets = [dict(bucket, key=key) for key, bucket in buckets.items()]
        for bucket in buckets:
            base = {name: bucket.get("key_as_string", bucket.get("key")), "doc_count": bucket.get("doc_count")}
            children = {k: v for k, v in bucket.items() if isinstance(v, dict) and k != "key"}
            child_rows, child_columns, child_groups = flatten_aggregations(children, children_spec)
            columns.update(child_columns)
            if child_groups:
                base[f"{name}.doc_count"] = base["doc_count"]
                columns[f"{name}.doc_count"] = None
            for child in child_rows if child_groups else (child_rows or [{}]):
                rows.append({**base, **child})
    if grouped:
        return [{**metrics, **row} for row in rows], columns, True
    return [metrics] if metrics else [], columns, False
