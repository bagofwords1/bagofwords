/**
 * Host side of the artifact app-data bridge (`useCollection`).
 *
 * The artifact iframe posts `APP_DATA_REQUEST`; the host page (never the
 * iframe) calls `/api/artifacts/{artifact_id}/data/...` with its OWN session
 * and answers `APP_DATA_RESULT` with the same `rid`. No credential ever enters
 * the iframe. `artifact_id` is the PARENT artifact id, shared by all versions.
 *
 * Server `error_code` values `app_data.<x>` become runtime `error.code` `<x>`
 * (`record_not_found` becomes `not_found`); host-only codes are `unavailable`,
 * `network` and `error`. This file is the only place hosts do that mapping.
 */

export type AppDataOp = 'list' | 'create' | 'update' | 'delete'
export interface AppDataRequest { type: 'APP_DATA_REQUEST'; rid: string; op: AppDataOp; collection: string; id?: string; data?: Record<string, unknown>; version?: number }
export interface AppDataError { code: string; message: string }
export interface AppDataResult { type: 'APP_DATA_RESULT'; rid: string; ok: boolean; items?: unknown[]; record?: unknown; error?: AppDataError }
export type AppDataFetch = (path: string, init: { method: 'GET' | 'POST' | 'PATCH' | 'DELETE'; body?: unknown }) => Promise<{ data: { value: any }; error: { value: any } }>

const OPS: ReadonlySet<string> = new Set(['list', 'create', 'update', 'delete'])

// Path segments are allowlisted, not just encoded: encodeURIComponent keeps
// "." and "..", and URL parsing removes dot segments, so an unchecked segment
// could steer the host's own session at other /api/artifacts/... endpoints.
// COLLECTION_NAME matches COLLECTION_NAME_PATTERN in
// backend/app/schemas/app_storage.py.
const COLLECTION_NAME = /^[a-z][a-z0-9_]{0,63}$/
const RECORD_ID = /^[A-Za-z0-9-]{1,64}$/

function validSegments(req: AppDataRequest): boolean {
  if (typeof req.collection !== 'string' || !COLLECTION_NAME.test(req.collection)) return false
  if (req.op === 'update' || req.op === 'delete') return typeof req.id === 'string' && RECORD_ID.test(req.id)
  return true
}

const CODE_BY_SERVER_CODE: Record<string, string> = {
  'app_data.unauthenticated': 'unauthenticated',
  'app_data.forbidden': 'forbidden',
  'app_data.collection_not_declared': 'collection_not_declared',
  'app_data.record_not_found': 'not_found',
  'app_data.validation': 'validation',
  'app_data.too_large': 'too_large',
  'app_data.limit_reached': 'limit_reached',
  'app_data.conflict': 'conflict',
  'artifact.not_found': 'not_found',
  'access.denied': 'forbidden',
}

const CODE_BY_STATUS: Record<number, string> = {
  401: 'unauthenticated',
  403: 'forbidden',
  404: 'not_found',
  409: 'conflict',
  413: 'too_large',
  422: 'validation',
}

const GENERIC_MESSAGE = 'App data request failed'

export function isAppDataRequest(msg: unknown): msg is AppDataRequest {
  if (!msg || typeof msg !== 'object') return false
  const m = msg as Record<string, unknown>
  return m.type === 'APP_DATA_REQUEST'
    && typeof m.rid === 'string' && m.rid.length > 0
    && typeof m.op === 'string' && OPS.has(m.op)
    && typeof m.collection === 'string' && m.collection.length > 0
}

export function appDataPath(artifactId: string, req: AppDataRequest): string {
  if (!validSegments(req)) throw new Error('Invalid app data collection or record id')
  const base = `/api/artifacts/${encodeURIComponent(artifactId)}/data/${encodeURIComponent(req.collection)}`
  return req.op === 'update' || req.op === 'delete' ? `${base}/${encodeURIComponent(String(req.id))}` : base
}

export function appDataErrorFrom(err: unknown, message?: (err: unknown) => string): AppDataError {
  const e = (err && typeof err === 'object' ? err : {}) as any
  const payload = e.data && typeof e.data === 'object' ? e.data : {}
  const status = Number(e.statusCode ?? e.status ?? e.response?.status) || undefined
  let code: string
  if (typeof payload.error_code === 'string' && CODE_BY_SERVER_CODE[payload.error_code]) {
    code = CODE_BY_SERVER_CODE[payload.error_code]
  } else if (status) {
    code = CODE_BY_STATUS[status] || 'error'
  } else {
    // An Error without an HTTP status never reached the server.
    code = err instanceof Error ? 'network' : 'error'
  }
  const text = (message && message(err))
    || (typeof payload.detail === 'string' && payload.detail)
    || (typeof e.message === 'string' && e.message)
    || GENERIC_MESSAGE
  return { code, message: text }
}

// Hosts wrap response bodies in Vue refs (reactive Proxies), which postMessage
// cannot clone. Records are JSON by contract, so a JSON copy is exact.
function plain<T>(value: T): T {
  return JSON.parse(JSON.stringify(value))
}

function failure(rid: string, code: string, message: string): AppDataResult {
  return { type: 'APP_DATA_RESULT', rid, ok: false, error: { code, message } }
}

export async function performAppDataRequest(req: AppDataRequest, opts: { artifactId: string | null | undefined; fetch: AppDataFetch; message?: (err: unknown) => string }): Promise<AppDataResult> {
  const { rid } = req
  if (!opts.artifactId) return failure(rid, 'unavailable', 'App data is not available here')
  if (!validSegments(req)) return failure(rid, 'validation', 'Invalid collection name or record id')
  const needsRecord = req.op === 'update' || req.op === 'delete'
  if (needsRecord && typeof req.version !== 'number') {
    return failure(rid, 'validation', 'A record id and version are required')
  }

  const path = appDataPath(opts.artifactId, req)
  let init: Parameters<AppDataFetch>[1]
  if (req.op === 'list') init = { method: 'GET' }
  else if (req.op === 'create') init = { method: 'POST', body: { data: req.data ?? {} } }
  else if (req.op === 'update') init = { method: 'PATCH', body: { data: req.data ?? {}, version: req.version } }
  else init = { method: 'DELETE', body: { version: req.version } }

  let response: Awaited<ReturnType<AppDataFetch>>
  try {
    response = await opts.fetch(path, init)
  } catch (err) {
    const error = appDataErrorFrom(err, opts.message)
    return failure(rid, error.code, error.message)
  }
  if (response.error?.value) {
    const error = appDataErrorFrom(response.error.value, opts.message)
    return failure(rid, error.code, error.message)
  }

  const value = response.data?.value
  if (req.op === 'list') {
    if (!value || !Array.isArray(value.items)) return failure(rid, 'error', 'Malformed app data response')
    return { type: 'APP_DATA_RESULT', rid, ok: true, items: plain(value.items) }
  }
  if (!value || typeof value !== 'object') return failure(rid, 'error', 'Malformed app data response')
  return { type: 'APP_DATA_RESULT', rid, ok: true, record: plain(value) }
}
