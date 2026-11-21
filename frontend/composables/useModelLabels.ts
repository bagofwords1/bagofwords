/** Model display names shared across home, the picker, and report navigation. */
export function useModelLabels() {
  const { organization } = useOrganization()
  const labels = useState<Record<string, Record<string, string>>>('model-display-labels', () => ({}))

  function rememberModels(models: Array<{ id?: string; model_id?: string; name?: string }>) {
    const orgId = organization.value?.id
    if (!orgId) return
    const next = { ...labels.value[orgId] }
    for (const model of models) {
      if (!model.name) continue
      if (model.id) next[model.id] = model.name
      if (model.model_id) next[model.model_id] = model.name
    }
    labels.value = { ...labels.value, [orgId]: next }
  }

  function hasModelLabel(id: string): boolean {
    const orgId = organization.value?.id
    return !!(orgId && labels.value[orgId]?.[id])
  }

  function modelLabel(id: string): string {
    const orgId = organization.value?.id
    // Never show a database UUID. Callers that can wait for the catalog should
    // check hasModelLabel() and render a placeholder instead of this fallback.
    return (orgId && labels.value[orgId]?.[id]) || 'BOW'
  }

  return { rememberModels, modelLabel, hasModelLabel }
}
