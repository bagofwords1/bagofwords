import { ref, readonly, onMounted } from 'vue'

const globalIsExcel = ref(false)
let isInitialized = false
let heartbeatTimeout: ReturnType<typeof setTimeout> | null = null

// How long to wait without a heartbeat before assuming we're no longer in Excel.
// The taskpane sends heartbeats every 5 s, so 12 s gives comfortable margin.
const HEARTBEAT_TIMEOUT_MS = 12_000

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
  if (process.client && !isInitialized) {
    // Remove old handler if exists (HMR safety)
    if (currentHandler) window.removeEventListener('message', currentHandler)
    currentHandler = handleExcelMessage
    window.addEventListener('message', handleExcelMessage, false)
    isInitialized = true
  }
}

export const useExcel = () => {
  const setExcelStatus = (value: boolean) => {
    globalIsExcel.value = value
    if (process.client) {
      if (value) {
        localStorage.setItem('excelStatus', JSON.stringify(true))
      } else {
        localStorage.removeItem('excelStatus')
      }
    }
  }

  const initExcelStatus = () => {
    if (process.client) {
      const storedStatus = localStorage.getItem('excelStatus')
      if (storedStatus !== null) {
        globalIsExcel.value = JSON.parse(storedStatus)
        // Start the timeout — if no heartbeat arrives, we'll clear the stale flag
        resetHeartbeatTimer()
      }
      setupExcelListener()
    }
  }

  onMounted(initExcelStatus)

  return {
    isExcel: readonly(globalIsExcel),
    excelSelection: readonly(globalExcelSelection),
    setExcelStatus
  }
}

export const isExcelSession = (): boolean => globalIsExcel.value
