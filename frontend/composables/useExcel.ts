import { ref, readonly, onMounted } from 'vue'

const globalIsExcel = ref(false)
let isInitialized = false
let heartbeatTimeout: ReturnType<typeof setTimeout> | null = null

// How long to wait without a heartbeat before assuming we're no longer in Excel.
// The taskpane sends heartbeats every 5 s, so 12 s gives comfortable margin.
const HEARTBEAT_TIMEOUT_MS = 12_000

function resetHeartbeatTimer() {
  if (heartbeatTimeout) clearTimeout(heartbeatTimeout)
  heartbeatTimeout = setTimeout(() => {
    globalIsExcel.value = false
    localStorage.removeItem('excelStatus')
  }, HEARTBEAT_TIMEOUT_MS)
}

function setupExcelListener() {
  if (process.client && !isInitialized) {
    window.addEventListener('message', (event) => {
      if (event.data?.type === 'excelInitialized') {
        globalIsExcel.value = true
        localStorage.setItem('excelStatus', JSON.stringify(true))
        resetHeartbeatTimer()
      }
    }, false)
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
    setExcelStatus
  }
}

export const isExcelSession = (): boolean => globalIsExcel.value
