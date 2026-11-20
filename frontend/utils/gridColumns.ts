// Column definitions for a query result shown in the shared data grid
// (RenderTable for chat / editor / panels, TableAgGrid for dashboards).
//
// `column_info` comes from the backend's pandas profile of the result:
// numeric dtypes get ag-grid's `numericColumn` type (cells align to the end
// edge, so digits line up; the column filter offers From / To when there are
// too many values to list). The stats line becomes part of the header tooltip.

const NUMERIC_DTYPE = /^(u?int|float|decimal)\d*$/i

export function isNumericDtype(dtype: unknown): boolean {
  return typeof dtype === 'string' && NUMERIC_DTYPE.test(dtype)
}

function statsFor(info: any): string {
  if (!info) return ''
  if (isNumericDtype(info.dtype)) {
    return `${info.dtype}\nmin: ${info.min}\nmax: ${info.max}\nmean: ${Number(info.mean).toFixed(2)}`
  }
  if (info.dtype === 'object') {
    return `${info.dtype}\nunique: ${info.unique_count}/${info.count}`
  }
  return ''
}

export function buildGridColumns(data: any, themeTokens: Record<string, any>): any[] {
  const columns = Array.isArray(data?.columns) ? data.columns : []
  return columns.map((col: any) => {
    const info = data?.info?.column_info?.[col.field]
    const numeric = isNumericDtype(info?.dtype)
    return {
      field: col.field,
      headerName: col.headerName,
      sortable: true,
      filter: true,
      ...(numeric ? { type: 'numericColumn' } : {}),
      headerComponentParams: {
        statsText: statsFor(info),
        themeTokens,
      },
      // Read the field as one literal key: result columns can be named
      // "a.b", which ag-grid's `field` would treat as a nested path.
      valueGetter: (params: any) => params.data?.[col.field],
    }
  })
}
