// Where a dashboard's back arrow (/d/{ref}) leads: back to the list the
// reader opened it from, named in the link (`?back=`), so it survives a
// reload. Only these lists are known: anything else in the parameter is
// ignored, so the link can never send a reader somewhere arbitrary.

export const BACK_PARAM = 'back'

export type BackOrigin = 'apps' | 'dashboards'
export type BackTarget = { origin: BackOrigin | 'home', path: string }

const ORIGINS: Record<BackOrigin, string> = { apps: '/apps', dashboards: '/dashboards' }

function originOf(raw: unknown): BackOrigin | null {
  const value = Array.isArray(raw) ? raw[0] : raw
  return typeof value === 'string' && Object.prototype.hasOwnProperty.call(ORIGINS, value)
    ? value as BackOrigin : null
}

/** `path` (a /d link), naming the list it is opened from. */
export function withBack(path: string, origin: BackOrigin): string {
  return `${path}${path.includes('?') ? '&' : '?'}${BACK_PARAM}=${origin}`
}

/** The `back` of the current link to carry over to another /d link (a
 *  redirect between the published page and the draft), or none. */
export function keepBack(path: string, raw: unknown): string {
  const origin = originOf(raw)
  return origin ? withBack(path, origin) : path
}

/** Where back leads. An app user has only the launcher (every workspace
 *  page turns them away); anyone else returns to the list they came from,
 *  or home. */
export function backTarget(raw: unknown, isAppUser: boolean): BackTarget {
  if (isAppUser) return { origin: 'apps', path: ORIGINS.apps }
  const origin = originOf(raw)
  return origin ? { origin, path: ORIGINS[origin] } : { origin: 'home', path: '/' }
}

/** The link itself: the page the reader actually came from (the router's
 *  `history.state.back`) when it is the target list, so its filters, search
 *  and tab come back with it; the bare list otherwise. */
export function backHref(target: BackTarget, previous: unknown): string {
  if (target.origin === 'home' || typeof previous !== 'string') return target.path
  return previous.split(/[?#]/)[0] === target.path ? previous : target.path
}
