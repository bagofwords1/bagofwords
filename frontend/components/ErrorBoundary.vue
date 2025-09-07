<template>
  <div v-if="hasError" class="error-boundary p-6 bg-red-50 border border-red-200 rounded-lg">
    <div class="flex items-center mb-4">
      <Icon name="heroicons:exclamation-triangle" class="w-6 h-6 text-red-500 mr-2" />
      <h3 class="text-lg font-semibold text-red-800">Something went wrong</h3>
    </div>
    
    <p class="text-red-700 mb-4">
      {{ errorMessage || 'An unexpected error occurred. Please try refreshing the page.' }}
    </p>
    
    <div class="flex space-x-3">
      <UButton 
        @click="retry" 
        color="red" 
        variant="solid"
        size="sm"
      >
        Try Again
      </UButton>
      
      <UButton 
        @click="refresh" 
        color="gray" 
        variant="outline"
        size="sm"
      >
        Refresh Page
      </UButton>
    </div>
    
    <details v-if="showDetails && errorDetails" class="mt-4">
      <summary class="cursor-pointer text-sm text-red-600 hover:text-red-800">
        Show Error Details
      </summary>
      <pre class="mt-2 p-3 bg-red-100 text-xs text-red-800 rounded overflow-auto">{{ errorDetails }}</pre>
    </details>
  </div>
  
  <slot v-else />
</template>

<script setup lang="ts">
interface Props {
  fallback?: string
  showDetails?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  fallback: '',
  showDetails: false
})

const emit = defineEmits<{
  error: [error: Error]
  retry: []
}>()

const hasError = ref(false)
const errorMessage = ref('')
const errorDetails = ref('')

const handleError = (error: Error) => {
  hasError.value = true
  errorMessage.value = props.fallback || error.message
  errorDetails.value = error.stack || error.toString()
  
  // Log error in development
  if (process.env.NODE_ENV === 'development') {
    console.error('Error Boundary caught error:', error)
  }
  
  emit('error', error)
}

const retry = () => {
  hasError.value = false
  errorMessage.value = ''
  errorDetails.value = ''
  emit('retry')
}

const refresh = () => {
  window.location.reload()
}

// Catch Vue errors
onErrorCaptured((error: Error) => {
  handleError(error)
  return false // Prevent error from propagating
})

// Catch unhandled promise rejections
if (process.client) {
  window.addEventListener('unhandledrejection', (event) => {
    handleError(new Error(event.reason))
  })
}

// Expose methods for parent components
defineExpose({
  handleError,
  retry,
  hasError: readonly(hasError)
})
</script>