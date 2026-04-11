import { ref, readonly, onMounted } from 'vue'

const globalIsExcel = ref(false)
let isInitialized = false // tracks whether ensureInitialized has run (reads storage, sets flags)
let listenerInstalled = false // tracks whether the window postMessage listener is attached
let heartbeatTimeout: ReturnType<typeof setTimeout> | null = null

// Sticky mode: once we know we're in Excel via the ?excel=true query param,
// stay in Excel mode for the life of the session regardless of postMessage
// heartbeat state. The query param is authoritative — it's set by the taskpane
// on the iframe src URL and doesn't depend on postMessage timing or hydration.
let isExcelSticky = false

// How long to wait without a heartbeat before assuming we're no longer in Excel.
// The taskpane sends heartbeats every 5 s, so 12 s gives comfortable margin.
// Only applies to the legacy postMessage-driven path; sticky mode never expires.
const HEARTBEAT_TIMEOUT_MS = 12_000

// Read the ?excel=true query param as early as possible — at module load time,
// before any middleware or navigation has a chance to rewrite the URL.
// Because nuxt.config.ts has `ssr: false`, this runs exactly once in the browser
// when the composable module is first imported. If a redirect later strips the
// query param from the URL, the sticky flag in localStorage still wins.
if (typeof window !== 'undefined') {
  try {
    const params = new URLSearchParams(window.location.search)
    const excelParam = params.get('excel')
    if (excelParam === 'true') {
      localStorage.setItem('excelStatus', JSON.stringify(true))
      localStorage.setItem('excelSticky', '1')
    } else if (excelParam === 'false') {
      // Explicit opt-out for local testing / debugging.
      localStorage.removeItem('excelStatus')
      localStorage.removeItem('excelSticky')
    }
  } catch {
    // localStorage unavailable (SSR, private mode, etc.) — ignore silently.
  }
}

export interface ExcelSelection {
  address: string
  sheetName: string
  selectionValues: any[][]
  cellCount: number
  totalCellCount: number
  truncated: boolean
  rowCount: number
  columnCount: number
}

const globalExcelSelection = ref<ExcelSelection | null>(null)

function resetHeartbeatTimer() {
  // Sticky mode (query-param-driven) never expires — skip the heartbeat entirely.
  if (isExcelSticky) {
    if (heartbeatTimeout) {
      clearTimeout(heartbeatTimeout)
      heartbeatTimeout = null
    }
    return
  }
  if (heartbeatTimeout) clearTimeout(heartbeatTimeout)
  heartbeatTimeout = setTimeout(() => {
    globalIsExcel.value = false
    globalExcelSelection.value = null
    localStorage.removeItem('excelStatus')
  }, HEARTBEAT_TIMEOUT_MS)
}

// Parse Excel address like "Sheet1!M12:Q27" or "A1" to compute cell count
function cellCountFromAddress(address: string): number {
  if (!address) return 0
  // Strip sheet name prefix (e.g. "Sheet1!A1:C3" -> "A1:C3")
  const range = address.replace(/^.*!/, '')
  const parts = range.split(':')
  if (parts.length === 1) return 1 // single cell

  const parseCell = (ref: string) => {
    const match = ref.match(/^([A-Z]+)(\d+)$/)
    if (!match) return { col: 0, row: 0 }
    let col = 0
    for (const ch of match[1]) col = col * 26 + (ch.charCodeAt(0) - 64)
    return { col, row: parseInt(match[2], 10) }
  }

  const start = parseCell(parts[0])
  const end = parseCell(parts[1])
  return Math.abs(end.col - start.col + 1) * Math.abs(end.row - start.row + 1)
}

// Extract selection from full sheet data using address range
// sheetData is 0-indexed from the used range start; address is absolute (e.g. "Sheet1!B3:D5")
// Since we don't know the used range offset, this is best-effort
function sliceSheetDataByAddress(sheetData: any[][], address: string): any[][] {
  if (!sheetData || !address) return []
  const range = address.replace(/^.*!/, '')
  const parts = range.split(':')

  const parseCell = (ref: string) => {
    const match = ref.match(/^([A-Z]+)(\d+)$/)
    if (!match) return { col: 0, row: 0 }
    let col = 0
    for (const ch of match[1]) col = col * 26 + (ch.charCodeAt(0) - 65) // 0-indexed
    return { col, row: parseInt(match[2], 10) - 1 } // 0-indexed
  }

  const start = parseCell(parts[0])
  const end = parts.length > 1 ? parseCell(parts[1]) : start

  // sheetData rows are from used range (row 0 = first used row)
  // We can't know the offset, so return sheetData as-is if selection spans beyond it
  const rows = sheetData.slice(start.row, end.row + 1)
  return rows.map(row => {
    if (!Array.isArray(row)) return [row]
    return row.slice(start.col, end.col + 1)
  })
}

let currentHandler: ((event: MessageEvent) => void) | null = null

function handleExcelMessage(event: MessageEvent) {
  if (event.data?.type === 'excelInitialized') {
    globalIsExcel.value = true
    localStorage.setItem('excelStatus', JSON.stringify(true))
    resetHeartbeatTimer()
  }
  if (event.data?.type === 'cellSelected') {
    if (!globalIsExcel.value) {
      globalIsExcel.value = true
      localStorage.setItem('excelStatus', JSON.stringify(true))
    }
    resetHeartbeatTimer()
    let vals = event.data.selectionValues || []
    if (vals.length === 0 && event.data.sheetData) {
      vals = sliceSheetDataByAddress(event.data.sheetData, event.data.address)
    }
    const cellCount = cellCountFromAddress(event.data.address)
    globalExcelSelection.value = {
      address: event.data.address,
      sheetName: event.data.sheetName,
      selectionValues: vals,
      cellCount: event.data.cellCount || cellCount,
      totalCellCount: event.data.totalCellCount || cellCount,
      truncated: !!event.data.truncated,
      rowCount: event.data.rowCount || vals.length,
      columnCount: event.data.columnCount || (vals[0]?.length || 0)
    }
  }
}

function setupExcelListener() {
  if (process.client && !listenerInstalled) {
    // Remove old handler if exists (HMR safety)
    if (currentHandler) window.removeEventListener('message', currentHandler)
    currentHandler = handleExcelMessage
    window.addEventListener('message', handleExcelMessage, false)
    listenerInstalled = true
  }
}

function ensureInitialized() {
  if (!process.client) return
  if (isInitialized) return
  isInitialized = true

  try {
    // 1) Sticky flag from localStorage (set either by the module-load query-param
    //    check above, or by a previous session that saw ?excel=true). This is
    //    authoritative — survives the 12 s heartbeat and client-side nav that
    //    may drop the query string from the URL.
    if (localStorage.getItem('excelSticky') === '1') {
      isExcelSticky = true
      globalIsExcel.value = true
    } else {
      // 2) Legacy postMessage-driven status. Starts the heartbeat as before —
      //    if no postMessage arrives within HEARTBEAT_TIMEOUT_MS, the flag
      //    clears, matching the old behavior for non-sticky sessions.
      const storedStatus = localStorage.getItem('excelStatus')
      if (storedStatus !== null) {
        globalIsExcel.value = JSON.parse(storedStatus)
        resetHeartbeatTimer()
      }
    }
  } catch {
    // localStorage unavailable — silently continue; postMessage listener still runs.
  }

  // 3) Install the postMessage listener regardless — it's still the only source
  //    of `cellSelected` data, and it's a belt-and-suspenders fallback for old
  //    taskpane builds that don't set the query param.
  setupExcelListener()
}

export const useExcel = () => {
  // Run sync init on every call; gated internally by `isInitialized` so it
  // only does real work once per session. This means any component that reads
  // `isExcel` gets the correct value immediately, without waiting for mount.
  ensureInitialized()

  const setExcelStatus = (value: boolean) => {
    globalIsExcel.value = value
    if (process.client) {
      if (value) {
        localStorage.setItem('excelStatus', JSON.stringify(true))
      } else {
        localStorage.removeItem('excelStatus')
        // Clearing status also clears sticky — callers that want to force-exit
        // Excel mode (e.g. debug tools) should see a clean slate.
        localStorage.removeItem('excelSticky')
        isExcelSticky = false
      }
    }
  }

  // Safety net: if somehow useExcel is called before `process.client` is true
  // (shouldn't happen with ssr: false, but cheap to keep), re-try on mount.
  onMounted(ensureInitialized)

  return {
    isExcel: readonly(globalIsExcel),
    excelSelection: readonly(globalExcelSelection),
    setExcelStatus
  }
}

export const isExcelSession = (): boolean => globalIsExcel.value
