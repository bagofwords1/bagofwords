interface AgentFolder {
  id: string
  name: string
  parent_id: string | null
  can_query: boolean
}

export interface AgentFolderOption {
  id: string
  name: string
  path: string
  parentPath: string
  ancestors: string[]
}

export function buildAgentFolderOptions<T extends {id: string, name: string, parent_id?: string | null}>(
  folders: T[], selectable: (folder: T) => boolean,
): AgentFolderOption[] {
  const byId = new Map(folders.map(folder => [folder.id, folder]))
  return folders.filter(selectable).map(folder => {
    const path = [folder.name]
    const ancestors = [folder.id]
    let parent = folder.parent_id && byId.get(folder.parent_id)
    while (parent && !ancestors.includes(parent.id)) {
      path.unshift(parent.name)
      ancestors.push(parent.id)
      parent = parent.parent_id && byId.get(parent.parent_id)
    }
    return { id: folder.id, name: folder.name, parentPath: path.slice(0, -1).join(' / '), path: path.join(' / '), ancestors }
  })
}

export async function loadAgentFolderOptions() {
  const { data, error } = await useMyFetch<{folders: AgentFolder[]}>('/folders')
  if (error.value) throw error.value
  const folders = data.value?.folders || []
  return buildAgentFolderOptions(folders, folder => folder.can_query)
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
