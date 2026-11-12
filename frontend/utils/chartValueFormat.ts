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
    if (v === null || v === undefined || v === '' || !Number.isFinite(n)) return v == null ? '' : String(v);
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

/** dataZoom for a crowded chart: open on the latest `zoomWindow` points, slider to the rest. */
export function latestWindowZoom(points: number, series: number, axisIndexKey: 'xAxisIndex' | 'yAxisIndex' = 'xAxisIndex') {
  const win = zoomWindow(points, series);
  if (!win) return null;
  const startValue = points - win, endValue = points - 1;
  return [
    { type: 'inside', [axisIndexKey]: 0, startValue, endValue, filterMode: 'weakFilter' },
    { type: 'slider', [axisIndexKey]: 0, startValue, endValue, bottom: 4, height: 18, brushSelect: false },
  ];
}
