import { computed, unref } from 'vue';
import { DEFAULT_THEME_NAME, themes } from '../themes/index';
import type { ThemeTokens } from '../themes/types';

export function useDashboardTheme(
  reportThemeName?: string | null | any,
  reportOverrides?: Record<string, any> | null | any,
  stepViewStyle?: Record<string, any> | null | any
) {
  const themeName = computed(() => {
    const name = String(unref(reportThemeName) || '').trim();
    return name && themes[name] ? name : DEFAULT_THEME_NAME;
  });

  const tokens = computed<ThemeTokens>(() => {
    const base = themes[themeName.value]?.tokens || themes[DEFAULT_THEME_NAME].tokens;
    // Shallow merge for now; deep-merge can be added when wiring
    const merged: any = { ...base };
    const ro = unref(reportOverrides) || {};
    if (ro && Object.keys(ro).length) {
      Object.assign(merged, ro);
    }
    const sv = unref(stepViewStyle) || {};
    if (sv && Object.keys(sv).length) {
      Object.assign(merged, sv);
    }
    return merged as ThemeTokens;
  });

  return { themeName, tokens };
}


