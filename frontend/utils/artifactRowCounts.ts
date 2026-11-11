/**
 * How much of a query's result a dataset holds, for window.ARTIFACT_DATA.
 *
 * Stored rows are cut at the org's row limit, so `rows.length` says nothing
 * about the size of the result; `info.total_rows` does. `truncated` tells
 * artifact code that totals computed from `rows` would be wrong.
 * Same rule as row_count_fields in backend/app/services/artifact_payload.py.
 */
export interface ArtifactRowCounts {
  row_count: number;
  loaded_row_count: number;
  truncated: boolean;
}

export function artifactRowCounts(data: any): ArtifactRowCounts {
  const loaded = Array.isArray(data?.rows) ? data.rows.length : 0;
  const total = data?.info?.total_rows;
  const real = Number.isInteger(total) && total >= loaded ? total : loaded;
  return { row_count: real, loaded_row_count: loaded, truncated: real > loaded };
}
