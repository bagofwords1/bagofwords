import { ref, readonly, onMounted } from 'vue'

const globalIsExcel = ref(false)
let isInitialized = false

function setupExcelListener() {
  if (process.client && !isInitialized) {
    window.addEventListener('message', (event) => {
      if (event.data.type === 'excelInitialized') {
        globalIsExcel.value = true
        localStorage.setItem('excelStatus', JSON.stringify(true))
      }
    }, false)
    isInitialized = true
  }
}

export const useExcel = () => {
  const setExcelStatus = (value: boolean) => {
    globalIsExcel.value = value
    if (process.client) {
      localStorage.setItem('excelStatus', JSON.stringify(value))
    }
  }

  const initExcelStatus = () => {
    if (process.client) {
      const storedStatus = localStorage.getItem('excelStatus')
      if (storedStatus !== null) {
        globalIsExcel.value = JSON.parse(storedStatus)
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
