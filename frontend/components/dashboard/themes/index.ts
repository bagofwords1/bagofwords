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
      axis: {
        xLabelColor: '#475569', xLineColor: '#e2e8f0',
        yLabelColor: '#475569', yLineColor: '#e2e8f0'
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
  midnight: {
    tokens: {
      // Neon gradients on deep midnight background
      palette: [
        { type: 'linear', x: 0, y: 0, x2: 1, y2: 0, colorStops: [ { offset: 0, color: '#22d3ee' }, { offset: 1, color: '#38bdf8' } ], global: false }, // cyan -> sky
        { type: 'linear', x: 0, y: 1, x2: 1, y2: 0, colorStops: [ { offset: 0, color: '#a78bfa' }, { offset: 1, color: '#6366f1' } ], global: false }, // violet -> indigo
        { type: 'linear', x: 1, y: 0, x2: 0, y2: 1, colorStops: [ { offset: 0, color: '#f472b6' }, { offset: 1, color: '#fb7185' } ], global: false }, // pink -> rose
        { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [ { offset: 0, color: '#34d399' }, { offset: 1, color: '#10b981' } ], global: false }, // emerald
        { type: 'linear', x: 0, y: 0, x2: 1, y2: 1, colorStops: [ { offset: 0, color: '#fbbf24' }, { offset: 1, color: '#f59e0b' } ], global: false }  // amber
      ] as any,
      background: '#0b1220',
      textColor: '#e2e8f0',
      cardBackground: '#0e1726',
      cardBorder: '#334155',
      axis: {
        xLabelColor: '#9fb3c8', xLineColor: '#1e293b',
        yLabelColor: '#9fb3c8', yLineColor: '#1e293b',
        gridLineColor: 'rgba(148, 163, 184, 0.15)'
      },
      legend: { textColor: '#cbd5e1' },
      grid: { top: '10%', bottom: '12%', left: '6%', right: '4%' },
      tooltip: { backgroundColor: 'rgba(2, 6, 23, 0.92)', borderColor: 'transparent', textStyle: { color: '#e2e8f0' } },
      animation: { duration: 550, easing: 'quartOut' }
    },
    componentOverrides: {
      'echarts.bar': { /* rounded bars styled by builder if needed */ },
      'echarts.line': { smooth: true }
    }
  }
};


