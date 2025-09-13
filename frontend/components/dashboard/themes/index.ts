import type { ThemeDefinition } from './types';

export const DEFAULT_THEME_NAME = 'default';

export const themes: Record<string, ThemeDefinition> = {
  default: {
    tokens: {
      // Vibrant, modern gradients for series colors
      palette: [
        { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [ { offset: 0, color: '#60a5fa' }, { offset: 1, color: '#2563eb' } ], global: false }, // blue
        { type: 'linear', x: 0, y: 0, x2: 1, y2: 0, colorStops: [ { offset: 0, color: '#34d399' }, { offset: 1, color: '#059669' } ], global: false }, // emerald
        { type: 'linear', x: 0, y: 0, x2: 1, y2: 1, colorStops: [ { offset: 0, color: '#fbbf24' }, { offset: 1, color: '#f59e0b' } ], global: false }, // amber
        { type: 'linear', x: 0, y: 1, x2: 1, y2: 0, colorStops: [ { offset: 0, color: '#fb7185' }, { offset: 1, color: '#ef4444' } ], global: false }, // rose/red
        { type: 'linear', x: 1, y: 0, x2: 0, y2: 1, colorStops: [ { offset: 0, color: '#a78bfa' }, { offset: 1, color: '#7c3aed' } ], global: false }  // violet
      ] as any,
      background: '#ffffff',
      textColor: '#0f172a',
      cardBackground: '#ffffff',
      cardBorder: '#e5e7eb',
      fontFamily: 'Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial',
      headingFontFamily: 'Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial',
      axis: {
        xLabelColor: '#475569', xLineColor: '#e2e8f0',
        yLabelColor: '#475569', yLineColor: '#e2e8f0',
        gridLineColor: '#e5e7eb',
        gridShow: true,
        xLabelShowAll: false, // Default to ECharts auto behavior
        xLabelRotate: 0,
        xLabelInterval: 'auto' as any
      },
      legend: { textColor: '#334155' },
      grid: { top: '10%', bottom: '12%', left: '6%', right: '4%' },
      tooltip: { backgroundColor: 'rgba(15, 23, 42, 0.9)', borderColor: 'transparent', textStyle: { color: '#e2e8f0' } },
      animation: { duration: 500, easing: 'cubicOut' }
    },
    componentOverrides: {
      'echarts.pie': { legend: { show: true } },
      'echarts.line': { smooth: true },
      'echarts.bar': { series: [{ itemStyle: { borderRadius: [4, 4, 0, 0] } }] }
    }
  },
  
  retro: {
    tokens: {
      // Warm retro palette inspired by 70s posters
      palette: [
        { type: 'linear', x: 0, y: 0, x2: 1, y2: 0, colorStops: [ { offset: 0, color: '#F59E0B' }, { offset: 1, color: '#D97706' } ], global: false }, // mustard -> burnt orange
        { type: 'linear', x: 0, y: 1, x2: 1, y2: 0, colorStops: [ { offset: 0, color: '#10B981' }, { offset: 1, color: '#047857' } ], global: false }, // avocado -> forest
        { type: 'linear', x: 0, y: 0, x2: 1, y2: 1, colorStops: [ { offset: 0, color: '#FCD34D' }, { offset: 1, color: '#F59E0B' } ], global: false }, // sunflower
        { type: 'linear', x: 1, y: 0, x2: 0, y2: 1, colorStops: [ { offset: 0, color: '#F472B6' }, { offset: 1, color: '#DB2777' } ], global: false }, // magenta
        { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [ { offset: 0, color: '#60A5FA' }, { offset: 1, color: '#2563EB' } ], global: false }, // cornflower -> royal
      ] as any,
      background: '#faf7f2',
      textColor: '#1f2937',
      cardBackground: '#fffaf3',
      cardBorder: '#e5d5b8',
      fontFamily: 'Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial',
      headingFontFamily: 'Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial',
      axis: {
        xLabelColor: '#6b7280', xLineColor: '#e5d5b8',
        yLabelColor: '#6b7280', yLineColor: '#e5d5b8',
        gridLineColor: 'rgba(125, 85, 44, 0.15)',
        gridShow: true,
        xLabelShowAll: false, // Keep retro classic with auto behavior
        xLabelRotate: 0,
        xLabelInterval: 'auto' as any
      },
      legend: { textColor: '#374151' },
      grid: { top: '10%', bottom: '12%', left: '6%', right: '4%' },
      tooltip: { backgroundColor: 'rgba(55, 65, 81, 0.95)', borderColor: 'transparent', textStyle: { color: '#FDE68A' } },
      animation: { duration: 600, easing: 'cubicBezier(0.33, 1, 0.68, 1)' }
    },
    componentOverrides: {
      'echarts.bar': { series: [{ itemStyle: { borderRadius: [6, 6, 0, 0] } }] },
      'echarts.line': { smooth: true }
    }
  },
  hacker: {
    tokens: {
      // Dark ops dashboard with neon accents (Grafana-esque)
      palette: [
        { type: 'linear', x: 0, y: 0, x2: 1, y2: 0, colorStops: [ { offset: 0, color: '#22c55e' }, { offset: 1, color: '#16a34a' } ], global: false }, // neon green
        { type: 'linear', x: 0, y: 0, x2: 1, y2: 1, colorStops: [ { offset: 0, color: '#06b6d4' }, { offset: 1, color: '#0ea5e9' } ], global: false }, // teal -> sky
        { type: 'linear', x: 0, y: 1, x2: 1, y2: 0, colorStops: [ { offset: 0, color: '#f97316' }, { offset: 1, color: '#fb923c' } ], global: false }, // orange
        { type: 'linear', x: 1, y: 0, x2: 0, y2: 1, colorStops: [ { offset: 0, color: '#a78bfa' }, { offset: 1, color: '#7c3aed' } ], global: false }, // violet
        { type: 'linear', x: 0, y: 0, x2: 1, y2: 1, colorStops: [ { offset: 0, color: '#eab308' }, { offset: 1, color: '#f59e0b' } ], global: false }, // amber
      ] as any,
      background: '#0a0f14',
      textColor: '#e2e8f0',
      cardBackground: '#0d141b',
      cardBorder: '#16212b',
      fontFamily: 'Courier New, Courier, ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, Liberation Mono, monospace',
      headingFontFamily: 'Courier New, Courier, ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, Liberation Mono, monospace',
      axis: {
        xLabelColor: '#94a3b8', xLineColor: '#16212b',
        yLabelColor: '#94a3b8', yLineColor: '#16212b',
        gridLineColor: 'rgba(148, 163, 184, 0.08)',
        gridShow: true,
        xLabelShowAll: false, // Hacker theme keeps it clean
        xLabelRotate: 0,
        xLabelInterval: 'auto' as any
      },
      legend: { textColor: '#cbd5e1' },
      grid: { top: '10%', bottom: '12%', left: '6%', right: '4%' },
      tooltip: { backgroundColor: 'rgba(3, 7, 18, 0.95)', borderColor: 'transparent', textStyle: { color: '#e2e8f0' } },
      animation: { duration: 600, easing: 'cubicOut' }
    },
    componentOverrides: {
      'echarts.bar': { series: [{ itemStyle: { borderRadius: [3, 3, 0, 0] } }] },
      'echarts.line': { smooth: true }
    }
  },
  research: {
    tokens: {
      // Clean academic vibe
      palette: [
        '#2563eb', '#0ea5e9', '#059669', '#7c3aed', '#f59e0b'
      ] as any,
      background: '#f7f7fb',
      textColor: '#0f172a',
      cardBackground: '#ffffff',
      cardBorder: '#e5e7eb',
      fontFamily: 'Times New Roman, Times, serif',
      headingFontFamily: 'Merriweather, Georgia, serif',
      axis: {
        xLabelColor: '#475569', xLineColor: '#e5e7eb',
        yLabelColor: '#475569', yLineColor: '#e5e7eb',
        gridLineColor: 'rgba(71, 85, 105, 0.15)',
        gridShow: true,
        xLabelShowAll: true, // Academic users often want to see all categories
        xLabelRotate: 45,    // Diagonal labels for better readability
        xLabelInterval: 0
      },
      legend: { textColor: '#334155' },
      grid: { top: '12%', bottom: '14%', left: '8%', right: '6%' },
      tooltip: { backgroundColor: 'rgba(15, 23, 42, 0.95)', borderColor: '#475569', textStyle: { color: '#e2e8f0' } },
      animation: { duration: 500, easing: 'cubicOut' }
    },
    componentOverrides: {
      'echarts.bar': { series: [{ itemStyle: { borderRadius: [2, 2, 0, 0] } }] },
      'echarts.line': { smooth: true }
    }
  }
};


