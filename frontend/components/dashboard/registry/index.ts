// Minimal dashboard component registry that reuses existing components.
// This keeps current components untouched and provides a single mapping
// from data_model.type to a component loader and prop builder.

type Loader = () => Promise<any>;

export type ComponentKey =
  | 'echarts.visual'
  | 'table.aggrid'
  | 'metric.card'
  | 'text.widget';

export type RegistryEntry = {
  componentKey: ComponentKey;
  load: Loader; // Lazy import of the Vue SFC
  supports: (type: string) => boolean;
  buildProps: (args: {
    widget?: any;
    step?: any;
    data?: any;
    data_model?: any;
  }) => Record<string, any>;
};

// Helper: normalize loosely emitted types
function normalizeType(raw?: string | null): string {
  const t = (raw || '').toLowerCase();
  if (t === 'pie') return 'pie_chart';
  if (t === 'bar') return 'bar_chart';
  if (t === 'line') return 'line_chart';
  if (t === 'area') return 'area_chart';
  return t;
}

// ECharts visual entry (uses existing RenderVisual.vue)
const echartsVisual: RegistryEntry = {
  componentKey: 'echarts.visual',
  load: () => import('../charts/EChartsVisual.vue'),
  supports: (type: string) => {
    const t = normalizeType(type);
    return [
      'bar_chart',
      'line_chart',
      'area_chart',
      'pie_chart',
      'scatter_plot',
      'heatmap',
      'map',
      'candlestick',
      'treemap',
      'radar_chart',
    ].includes(t);
  },
  buildProps: ({ widget, data, data_model }) => ({
    widget,
    data,
    data_model,
  }),
};

// Table entry (uses existing RenderTable.vue)
const tableAgGrid: RegistryEntry = {
  componentKey: 'table.aggrid',
  load: () => import('../table/TableAgGrid.vue'),
  supports: (type: string) => normalizeType(type) === 'table',
  buildProps: ({ widget, step }) => ({ widget, step }),
};

// Metric card entry (rich KPI with trends) - also handles legacy "count" type
const metricCard: RegistryEntry = {
  componentKey: 'metric.card',
  load: () => import('../kpi/MetricCard.vue'),
  supports: (type: string) => {
    const t = normalizeType(type);
    return t === 'metric_card' || t === 'count';
  },
  buildProps: ({ widget, step, data, data_model }) => ({
    widget,
    step,
    data,
    data_model,
  }),
};

// Text widget entry
const textWidgetEntry: RegistryEntry = {
  componentKey: 'text.widget',
  load: () => import('../text/TextWidget.vue'),
  supports: (type: string) => normalizeType(type) === 'text_widget',
  buildProps: ({ widget, step }) => ({ widget, step }),
};

export const registry: RegistryEntry[] = [
  echartsVisual,
  tableAgGrid,
  metricCard,
  textWidgetEntry,
];

export function resolveEntryByType(type?: string | null): RegistryEntry | null {
  const t = normalizeType(type);
  for (const entry of registry) {
    if (entry.supports(t)) return entry;
  }
  return null;
}


