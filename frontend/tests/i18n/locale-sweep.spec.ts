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

type Locale = 'en' | 'es' | 'he' | 'fr' | 'sv' | 'ar' | 'ru' | 'de' | 'pt' | 'it';

const CASES: Record<Locale, {
  dir: 'ltr' | 'rtl';
  // Substrings expected on /i18n-smoke for that locale
  smokeHello: string;
  smokeCommonSave: string;
  // Pattern expected on the /users/sign-in heading, which renders
  // auth.welcomeTitle. A regex so a locale rewrite can pick a synonym.
  signInHeading: RegExp;
}> = {
  en: {
    dir: 'ltr',
    smokeHello: 'Hello',
    smokeCommonSave: 'Save',
    signInHeading: /Welcome to BOW/,
  },
  es: {
    dir: 'ltr',
    smokeHello: 'Hola',
    smokeCommonSave: 'Guardar',
    signInHeading: /Bienvenido a BOW/,
  },
  he: {
    dir: 'rtl',
    smokeHello: 'שלום',
    smokeCommonSave: 'שמירה',
    signInHeading: /ברוכים הבאים/,
  },
  fr: {
    dir: 'ltr',
    smokeHello: 'Bonjour',
    smokeCommonSave: 'Enregistrer',
    signInHeading: /Bienvenue sur BOW/,
  },
  sv: {
    dir: 'ltr',
    smokeHello: 'Hej',
    smokeCommonSave: 'Spara',
    signInHeading: /Välkommen till BOW/,
  },
  ar: {
    dir: 'rtl',
    smokeHello: 'مرحبًا',
    smokeCommonSave: 'حفظ',
    signInHeading: /مرحبًا بك في BOW/,
  },
  ru: {
    dir: 'ltr',
    smokeHello: 'Привет',
    smokeCommonSave: 'Сохранить',
    signInHeading: /Добро пожаловать в BOW/,
  },
  de: {
    dir: 'ltr',
    smokeHello: 'Hallo',
    smokeCommonSave: 'Speichern',
    signInHeading: /Willkommen bei BOW/,
  },
  pt: {
    dir: 'ltr',
    smokeHello: 'Olá',
    smokeCommonSave: 'Salvar',
    signInHeading: /Bem-vindo ao BOW/,
  },
  it: {
    dir: 'ltr',
    smokeHello: 'Ciao',
    smokeCommonSave: 'Salva',
    signInHeading: /Benvenuto in BOW/,
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
  // The browser 'load'/'networkidle' events are unreliable on CI (goto can hang
  // until the per-test timeout). Commit the navigation, then gate on the SPA
  // having booted — the i18n plugin sets <html lang> once Nuxt has hydrated —
  // so the assertions that follow run against a ready page.
  await page.goto(url, { waitUntil: 'commit' });
  await expect(page.locator('html')).toHaveAttribute('lang', locale, { timeout: 20000 });
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
      await expect(page.locator('h1').first()).toContainText(expected.signInHeading, { timeout: 15000 });
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
