/**
 * Composable for handling API errors consistently
 */
export const useApiError = () => {
  const toast = useToast()

  const handleError = (error: any, context?: string) => {
    let message = 'An unexpected error occurred'
    let title = 'Error'

    if (error?.data?.detail) {
      message = error.data.detail
    } else if (error?.message) {
      message = error.message
    } else if (typeof error === 'string') {
      message = error
    }

    // Handle specific error types
    if (error?.status === 401) {
      title = 'Authentication Error'
      message = 'Please sign in to continue'
    } else if (error?.status === 403) {
      title = 'Permission Denied'
      message = 'You do not have permission to perform this action'
    } else if (error?.status === 404) {
      title = 'Not Found'
      message = 'The requested resource was not found'
    } else if (error?.status === 429) {
      title = 'Rate Limited'
      message = 'Too many requests. Please try again later'
    } else if (error?.status >= 500) {
      title = 'Server Error'
      message = 'A server error occurred. Please try again later'
    }

    // Add context if provided
    if (context) {
      title = `${title} - ${context}`
    }

    toast.add({
      title,
      description: message,
      color: 'red',
      timeout: 5000
    })

    // Log error in development
    if (process.env.NODE_ENV === 'development') {
      console.error('API Error:', error)
    }
  }

  const handleSuccess = (message: string, title: string = 'Success') => {
    toast.add({
      title,
      description: message,
      color: 'green',
      timeout: 3000
    })
  }

  return {
    handleError,
    handleSuccess
  }
}