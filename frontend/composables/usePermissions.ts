export const usePermissions = () => {
  return useState<string[]>('permissions', () => [])
}

export const usePermissionsLoaded = () => {
  return useState<boolean>('permissionsLoaded', () => false)
}

// Add the useCan function to check permissions
export const useCan = (permission: string) => {
  const permissions = usePermissions()
  const permissionsLoaded = usePermissionsLoaded()
  
  // Only return true if permissions are loaded and permission exists
  return permissionsLoaded.value && permissions.value.includes(permission)
}
