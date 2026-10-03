// Intercom's launcher, in both of the shapes Intercom renders it: the
// lightweight placeholder shown while the messenger loads, and the iframe that
// replaces it. These are Intercom's own class/name hooks — if they ever change,
// nothing matches and pages simply stop making room (today's behavior).
export const INTERCOM_LAUNCHER_SELECTOR = [
  '.intercom-lightweight-app-launcher',
  '.intercom-launcher-frame',
  'iframe[name="intercom-launcher-frame"]',
].join(', ')

type Rect = { top: number; height: number }

// How far up from the viewport bottom the launcher reaches (px), or 0 when no
// launcher is on screen. Hidden launchers (hide_default_launcher, display:none)
// measure 0 tall and are ignored; a launcher scrolled or animated below the
// fold contributes nothing.
export function launcherClearance(rects: Rect[], viewportHeight: number): number {
  let clearance = 0
  for (const rect of rects) {
    if (!(rect.height > 0)) continue
    clearance = Math.max(clearance, viewportHeight - rect.top)
  }
  return Math.max(0, Math.round(clearance))
}
