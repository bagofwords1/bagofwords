/**
 * Number format for a stored chart's value axis and tooltip, and the window a
 * crowded chart opens on. Both renderers (RenderVisual, EChartsVisual) use it.
 *
 * `format` comes from the view's axisY (set by create_data from the inferred
 * `display`): "currency" (with an ISO code), "percent" (the value is already a
 * percentage: 12.5 → "12.5%", like metric cards), "compact" or "number".
 * Nothing stored means plain numbers, as before.
 */
export interface ValueFormat { format?: string | null; currency?: string | null }

export function valueFormatter(f?: ValueFormat | null): ((v: unknown) => string) | undefined {
  const fmt = String(f?.format || '').toLowerCase();
  if (fmt !== 'currency' && fmt !== 'percent' && fmt !== 'compact') return undefined;
  const currency = /^[A-Za-z]{3}$/.test(String(f?.currency || '')) ? String(f!.currency).toUpperCase() : 'USD';
  return (v: unknown): string => {
    const n = typeof v === 'number' ? v : Number(v);
    if (v === null || v === undefined || v === '' || !Number.isFinite(n)) return v == null || v === '' ? '-' : String(v); // '-' as ECharts prints a missing value
    const abs = Math.abs(n);
    try {
      if (fmt === 'currency') {
        return new Intl.NumberFormat('en-US', {
          style: 'currency', currency,
          notation: abs >= 1e6 ? 'compact' : 'standard',
          minimumFractionDigits: 0, maximumFractionDigits: abs >= 1e6 ? 1 : abs >= 100 ? 0 : 2,
        }).format(n);
      }
      if (fmt === 'percent') {
        return new Intl.NumberFormat('en-US', { style: 'percent', minimumFractionDigits: 0, maximumFractionDigits: 1 }).format(n / 100);
      }
      return new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(n);
    } catch {
      return String(n);
    }
  };
}

/** Points shown first when a chart opens zoomed: 24 with several series, 60 with one. */
export function zoomWindow(points: number, series: number): number | null {
  const cap = series > 1 ? 24 : 60;
  return points > cap ? cap : null;
}

/** A sortable key for a time-like category label (date, year, YYYY-MM, quarter), else null. */
function timeKey(label: unknown): number | null {
  const s = String(label ?? '').trim();
  let m = s.match(/^(\d{4})[-\s]?Q([1-4])$/i) || s.match(/^Q([1-4])[-\s]?(\d{4})$/i);
  if (m) { const [y, q] = /^\d{4}$/.test(m[1]) ? [m[1], m[2]] : [m[2], m[1]]; return Number(y) * 4 + Number(q); }
  if (/^\d{4}$/.test(s)) return Number(s) * 12 * 31;
  if (/^\d{4}[-/]\d{1,2}([-/]\d{1,2})?([T ].*)?$/.test(s)) { const t = Date.parse(s.length === 7 ? s + '-01' : s); return Number.isNaN(t) ? null : t; }
  return null;
}

/** True when time-like categories run newest first (rows ordered DESC). */
export function isNewestFirst(categories: unknown[]): boolean {
  if (!categories || categories.length < 2) return false;
  const a = timeKey(categories[0]), b = timeKey(categories[categories.length - 1]);
  return a !== null && b !== null && a > b;
}

/** dataZoom for a crowded chart: open on the latest `zoomWindow` points, slider to the rest.
 *  The latest points are at the end of oldest-first categories and at the start of newest-first ones. */
export function latestWindowZoom(categories: unknown[] | number, series: number, axisIndexKey: 'xAxisIndex' | 'yAxisIndex' = 'xAxisIndex') {
  const points = Array.isArray(categories) ? categories.length : categories;
  const win = zoomWindow(points, series);
  if (!win) return null;
  const newestFirst = Array.isArray(categories) && isNewestFirst(categories);
  const startValue = newestFirst ? 0 : points - win, endValue = newestFirst ? win - 1 : points - 1;
  return [
    { type: 'inside', [axisIndexKey]: 0, startValue, endValue, filterMode: 'weakFilter' },
    { type: 'slider', [axisIndexKey]: 0, startValue, endValue, bottom: 4, height: 18, brushSelect: false },
  ];
}
