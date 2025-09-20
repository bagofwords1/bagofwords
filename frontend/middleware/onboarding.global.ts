//

export default (defineNuxtRouteMiddleware(async (to) => {
  const { data: currentUser } = useAuth()
  const { organization, ensureOrganization } = useOrganization()
  const { onboarding, fetchOnboarding } = useOnboarding()

  // Special handling for onboarding routes: if completed, redirect to home
  if (to.path.startsWith('/onboarding')) {
    await ensureOrganization()
    await fetchOnboarding({ in_onboarding: true })
    const ob = onboarding.value
    if (ob?.completed) return navigateTo('/')
    return
  }

  // Allow auth and organization creation routes
  const allowPrefixes = ['/users/', '/organizations/new']
  if (allowPrefixes.some(p => to.path.startsWith(p))) return

  // Ensure org
  await ensureOrganization()
  if (!organization.value?.id) return

  // Only nudge admins
  // Find role from session organizations list if available
  const org = ((currentUser.value as any)?.organizations || []).find((o: any) => o.id === organization.value?.id)
  const isAdmin = org?.role === 'admin'
  if (!isAdmin) return

  // Fetch onboarding and redirect if needed
  await fetchOnboarding({ in_onboarding: false })
  const ob = onboarding.value
  if (!ob) return
  if (!ob.completed && !ob.dismissed) {
    return navigateTo('/onboarding')
  }
}))


