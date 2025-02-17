export const usePermissions = () => {
  return useState<string[]>('permissions', () => [])
}

// Add the useCan function to check permissions
export const useCan = (permission: string) => {
  const permissions = usePermissions()
  return permissions.value.includes(permission)
}
