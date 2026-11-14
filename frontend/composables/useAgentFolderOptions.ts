interface AgentFolder {
  id: string
  name: string
  parent_id: string | null
  can_query: boolean
}

export async function loadAgentFolderOptions() {
  const { data, error } = await useMyFetch<{folders: AgentFolder[]}>('/folders')
  if (error.value) throw error.value
  const folders = data.value?.folders || []
  const byId = new Map(folders.map(folder => [folder.id, folder]))
  return folders.filter(folder => folder.can_query).map(folder => {
    const path = [folder.name]
    const ancestors = [folder.id]
    let parent = folder.parent_id && byId.get(folder.parent_id)
    while (parent && !ancestors.includes(parent.id)) {
      path.unshift(parent.name)
      ancestors.push(parent.id)
      parent = parent.parent_id && byId.get(parent.parent_id)
    }
    return { id: folder.id, path: path.join(' / '), ancestors }
  })
}

export function channelScopeValues(agents: string[] = [], folders: string[] = []) {
  return [...folders.map(id => 'folder:' + id), ...agents]
}

export function channelScopePayload(values: string[]) {
  return {
    agent_ids: values.filter(id => !id.startsWith('folder:')),
    agent_folder_ids: values.filter(id => id.startsWith('folder:')).map(id => id.slice(7)),
  }
}
