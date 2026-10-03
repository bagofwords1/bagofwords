// Audit Log Streams Composable
// Licensed under the BOW Enterprise License

export type StreamDestination = 'datadog' | 'splunk' | 'sentinel' | 's3' | 'gcs' | 'https' | 'syslog'
export type StreamState = 'active' | 'inactive' | 'error' | 'invalid'

export type DestinationField = {
  key: string
  kind: 'text' | 'url' | 'number' | 'bool' | 'select' | 'textarea' | 'headers'
  required: boolean
  secret: boolean
  default: any
  options: string[]
  advanced: boolean
}

export type DestinationSpec = { type: StreamDestination; fields: DestinationField[] }

export type AuditStream = {
  id: string
  name: string
  destination: StreamDestination
  config: Record<string, any>
  secrets: Record<string, string>
  action_filter: string[] | null
  state: StreamState
  start_from: 'now' | 'beginning'
  delivered_count: number
  last_delivered_at: string | null
  last_attempt_at: string | null
  last_error: string | null
  consecutive_failures: number
  next_attempt_at: string | null
  created_at: string | null
  status: { pending: number; lag_seconds: number }
}

export type StreamPayload = {
  name?: string
  destination?: StreamDestination
  config?: Record<string, any>
  secrets?: Record<string, any>
  action_filter?: string[] | null
  start_from?: 'now' | 'beginning'
  state?: 'active' | 'inactive'
  activate?: boolean
}

export type StreamTestResult = { ok: boolean; kind: string; error: string | null; status: number | null }

const BASE = '/api/enterprise/audit/streams'

export const useAuditStreams = () => {
  const streams = ref<AuditStream[]>([])
  const destinations = ref<DestinationSpec[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)
  const { getErrorMessage } = useErrorMessage()

  const call = async <T>(url: string, opts: Record<string, any> = {}): Promise<T> => {
    const res = await useMyFetch(url, opts)
    if (res.status.value !== 'success') throw res.error?.value
    return res.data.value as T
  }

  const fetchStreams = async () => {
    loading.value = true
    error.value = null
    try {
      streams.value = await call<AuditStream[]>(BASE)
    } catch (e) {
      error.value = getErrorMessage(e)
    } finally {
      loading.value = false
    }
  }

  const fetchDestinations = async () => {
    if (destinations.value.length) return
    try {
      destinations.value = await call<DestinationSpec[]>(`${BASE}/destinations`)
    } catch (e) {
      error.value = getErrorMessage(e)
    }
  }

  const createStream = (body: StreamPayload) => call<AuditStream>(BASE, { method: 'POST', body })
  const updateStream = (id: string, body: StreamPayload) => call<AuditStream>(`${BASE}/${id}`, { method: 'PATCH', body })
  const deleteStream = (id: string) => call<null>(`${BASE}/${id}`, { method: 'DELETE' })
  const testStream = (body: { destination: string; config: Record<string, any>; secrets: Record<string, any>; stream_id?: string }) =>
    call<StreamTestResult>(`${BASE}/test`, { method: 'POST', body })

  return {
    streams, destinations, loading, error, getErrorMessage,
    fetchStreams, fetchDestinations, createStream, updateStream, deleteStream, testStream,
  }
}
