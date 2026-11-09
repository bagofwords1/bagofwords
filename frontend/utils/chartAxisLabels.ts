/**
 * How many category labels a chart prints, and at what angle.
 *
 * A saved chart keeps what it stored. A chart that stored nothing gets the
 * automatic rule, which thins labels as categories grow, so a 60-month chart
 * doesn't try to print 60 dates.
 */
export interface AxisLabelSettings {
  rotate?: number | null;
  interval?: number | null;
}

export interface AxisLabelConfig {
  interval: number;
  rotate: number;
  hideOverlap: boolean;
}

export function autoAxisLabels(numCategories: number): AxisLabelConfig {
  if (numCategories > 50) return { interval: Math.max(1, Math.floor(numCategories / 20)), rotate: 45, hideOverlap: true };
  if (numCategories > 10) return { interval: 1, rotate: 45, hideOverlap: true };
  if (numCategories > 5) return { interval: 0, rotate: 45, hideOverlap: true };
  // Upright for a handful of categories; ECharts still drops any that overlap
  // on a narrow chart, and on a wide one nothing is hidden.
  return { interval: 0, rotate: 0, hideOverlap: true };
}

export function resolveAxisLabels(axis: AxisLabelSettings | null | undefined, numCategories: number): AxisLabelConfig {
  const auto = autoAxisLabels(numCategories);
  return { rotate: axis?.rotate ?? auto.rotate, interval: axis?.interval ?? auto.interval, hideOverlap: auto.hideOverlap };
}
