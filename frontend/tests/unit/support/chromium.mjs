// The Chromium the browser tests launch: PW_CHROMIUM_PATH, or the older
// PW_CHROMIUM / BOW_CHROMIUM_EXECUTABLE, or (undefined) the build the pinned
// Playwright downloads. One place, so every test honours the same variable.
export const CHROME_PATH = process.env.PW_CHROMIUM_PATH || process.env.PW_CHROMIUM || process.env.BOW_CHROMIUM_EXECUTABLE || undefined;
