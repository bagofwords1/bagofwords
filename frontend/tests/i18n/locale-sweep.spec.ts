/**
 * Phase 8 locale sweep: per-locale smoke checks on the unauthenticated
 * routes. Each test seeds `localStorage.bow.locale` so the plugin picks
 * up the target locale on first render, then asserts:
 *
 *   - <html lang="…" dir="…"> flips correctly (rtl only for `he`)
 *   - known strings render in the target language
 *   - no unresolved {{ }} or vue-i18n missing-key indicators leak
 *   - no `[intlify]` console warnings
 *
 * These don't depend on a seeded admin/org because the pages under test
 * are `definePageMeta({ auth: false })` — the backend's /api/config/i18n
 * fetch may fail, which is harmless (plugin swallows the error).
 */
import { test, expect, type Page, type ConsoleMessage } from '@playwright/test';

type Locale = 'en' | 'es' | 'he';

const CASES: Record<Locale, {
  dir: 'ltr' | 'rtl';
  // Substrings expected on /i18n-smoke for that locale
  smokeHello: string;
  smokeCommonSave: string;
  // Pattern expected on the /users/sign-in heading. A regex because English
  // authoring keeps two parallel keys (auth.signIn = "Login", auth.login =
  // "Sign in") and the page uses auth.signIn today. Locale rewrites are
  // allowed to pick either synonym.
  signInHeading: RegExp;
}> = {
  en: {
    dir: 'ltr',
    smokeHello: 'Hello',
    smokeCommonSave: 'Save',
    signInHeading: /Login|Sign in/,
  },
  es: {
    dir: 'ltr',
    smokeHello: 'Hola',
    smokeCommonSave: 'Guardar',
    signInHeading: /Iniciar sesión/,
  },
  he: {
    dir: 'rtl',
    smokeHello: 'שלום',
    smokeCommonSave: 'שמירה',
    signInHeading: /התחברות|כניסה/,
  },
};

/**
 * Seeds the target locale into localStorage before the page boots. We use
 * addInitScript so the value is set BEFORE the Nuxt plugin reads it during
 * the very first render. Navigating to /i18n-smoke first (a tiny public
 * page) lets us set the origin before the real target page loads.
 */
async function gotoWithLocale(page: Page, url: string, locale: Locale) {
  await page.addInitScript((loc) => {
    try { localStorage.setItem('bow.locale', loc); } catch {}
  }, locale);
  await page.goto(url);
}

async function expectNoRawI18nArtifacts(page: Page) {
  const text = await page.locator('body').innerText();
  expect(text, 'raw "{{ … }}" leaked into visible text').not.toMatch(/\{\{[\s\S]*?\}\}/);
  // vue-i18n by default renders the key path for missing keys. The prefix
  // "common.", "nav.", "settings." etc. would only appear as-is when the
  // key wasn't resolved. We flag the obvious shapes.
  expect(text, 'unresolved key shape in visible text').not.toMatch(/\b(common|nav|settings|errors)\.[a-zA-Z_]+\b/);
}

function collectIntlifyWarnings(page: Page): { warnings: string[] } {
  const warnings: string[] = [];
  page.on('console', (msg: ConsoleMessage) => {
    const text = msg.text();
    if (/\[intlify\]/.test(text)) warnings.push(text);
  });
  return { warnings };
}

for (const [locale, expected] of Object.entries(CASES) as [Locale, typeof CASES[Locale]][]) {
  test.describe(`locale=${locale}`, () => {
    test.use({ storageState: { cookies: [], origins: [] } });

    test(`i18n-smoke page renders in ${locale} with correct dir`, async ({ page }) => {
      const { warnings } = collectIntlifyWarnings(page);
      await gotoWithLocale(page, '/i18n-smoke', locale);
      await expect(page.locator('[data-test="smoke-locale"]')).toHaveText(locale);
      await expect(page.locator('html')).toHaveAttribute('lang', locale);
      await expect(page.locator('html')).toHaveAttribute('dir', expected.dir);
      await expect(page.locator('[data-test="smoke-hello"]')).toContainText(expected.smokeHello);
      const body = await page.locator('body').innerText();
      expect(body).toContain(expected.smokeCommonSave);
      await expectNoRawI18nArtifacts(page);
      expect(warnings, `vue-i18n warnings: ${warnings.join(' | ')}`).toEqual([]);
    });

    test(`sign-in page renders in ${locale}`, async ({ page }) => {
      const { warnings } = collectIntlifyWarnings(page);
      await gotoWithLocale(page, '/users/sign-in', locale);
      await expect(page.locator('html')).toHaveAttribute('lang', locale);
      await expect(page.locator('html')).toHaveAttribute('dir', expected.dir);
      // Heading is an h1 that interpolates $t('auth.signIn')
      await expect(page.locator('h1').first()).toContainText(expected.signInHeading);
      await expectNoRawI18nArtifacts(page);
      expect(warnings, `vue-i18n warnings: ${warnings.join(' | ')}`).toEqual([]);
    });

    test(`locale switcher flips dir when moving from en to ${locale}`, async ({ page }) => {
      await gotoWithLocale(page, '/i18n-smoke', 'en');
      await expect(page.locator('html')).toHaveAttribute('dir', 'ltr');
      // Click the target-locale button on the smoke page
      await page.locator('[data-test="smoke-buttons"] button', { hasText: locale }).click();
      await expect(page.locator('html')).toHaveAttribute('lang', locale);
      await expect(page.locator('html')).toHaveAttribute('dir', expected.dir);
      await expect(page.locator('[data-test="smoke-hello"]')).toContainText(expected.smokeHello);
    });
  });
}
