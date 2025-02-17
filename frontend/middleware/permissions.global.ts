import { useCan } from '~/composables/usePermissions'
import { useNuxtApp } from '#app'

export default defineNuxtRouteMiddleware(async (to, from) => {
  const pageMeta = to.meta
  const requiredPermissions = pageMeta.permissions || []
  
  // Check if user has all required permissions
  // promise wait 100
  
  let hasPermission = true
  for (const permission of requiredPermissions) {
    const can = useCan(permission)
    if (!can) {
      hasPermission = false
      break
    }
  }

  if (!hasPermission) {
    //console.warn('User does not have the required permissions:', requiredPermissions)
    //return navigateTo('/')
  }
})
