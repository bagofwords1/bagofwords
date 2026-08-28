import { computed, unref } from 'vue';
import { useColorMode } from '#imports';
import { DARK_DEFAULT_TOKENS, DEFAULT_THEME_NAME, themes } from '../themes/index';
import type { ThemeTokens } from '../themes/types';

export function useDashboardTheme(
  reportThemeName?: string | null | any,
  reportOverrides?: Record<string, any> | null | any,
  stepViewStyle?: Record<string, any> | null | any
) {
  // The app color mode ('light' | 'dark'). Guarded so this composable stays
  // usable outside a Nuxt setup context (unit tests, SSR edge cases).
  let colorMode: { value?: string } | null = null;
  try {
    colorMode = useColorMode();
  } catch {
    colorMode = null;
  }

  const themeName = computed(() => {
    const name = String(unref(reportThemeName) || '').trim();
    return name && themes[name] ? name : DEFAULT_THEME_NAME;
  });

  const tokens = computed<ThemeTokens>(() => {
    // The default theme follows the app color mode; explicitly themed reports
    // (retro, hacker, ...) keep their chosen look in either mode.
    const isDark = colorMode?.value === 'dark';
    const base = themeName.value === DEFAULT_THEME_NAME && isDark
      ? DARK_DEFAULT_TOKENS
      : (themes[themeName.value]?.tokens || themes[DEFAULT_THEME_NAME].tokens);
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
