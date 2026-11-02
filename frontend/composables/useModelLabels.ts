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

  function modelLabel(id: string): string {
    const orgId = organization.value?.id
    // Never flash a database UUID while the catalog is loading.
    return (orgId && labels.value[orgId]?.[id]) || 'BoW'
  }

  return { rememberModels, modelLabel }
}
