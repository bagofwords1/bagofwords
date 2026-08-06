// Shared project (folder) state for the sidebar and project pages.
// Projects are private-by-default shared folders that group reports.

export interface Project {
    id: string
    name: string
    description?: string | null
    color?: string | null
    access: 'private' | 'org'
    user_id: string
    report_count: number
    member_count: number
    is_owner: boolean
    can_manage: boolean
}

const projects = ref<Project[]>([])
const loaded = ref(false)
const loading = ref(false)

// Every "New report" entry point creates the report server-side before
// navigating, and the backend copies the project's default agents onto it —
// but only when the caller passes `project_id` and *no* explicit agents (an
// explicit selection overrides the defaults instead of being union-ed with
// them). The global entry points otherwise send `useAgent`'s workspace-wide
// selection, which falls back to every org agent, so a report started from
// inside a project came up with the whole org attached instead of the agents
// the project rail promises. Callers use this to build the right payload.
export function useNewReportProjectContext() {
    const route = useRoute()
    const activeProjectId = computed(() => projectIdFromPath(route.path))

    // `explicit` are agents the user genuinely picked for this report (e.g. a
    // prompt's data-source mentions). Inside a project, an empty pick means
    // "use the project's defaults" — never the workspace-wide fallback.
    const newReportPayload = (explicit: string[] = []) => {
        const projectId = activeProjectId.value
        if (!projectId) return { data_sources: explicit }
        return { data_sources: explicit, project_id: projectId }
    }

    return { activeProjectId, newReportPayload }
}

export function useProjects() {
    const fetchProjects = async () => {
        if (loading.value) return
        loading.value = true
        try {
            const resp: any = await useMyFetch('/projects', { method: 'GET' })
            if (resp?.status?.value === 'success' && resp.data?.value?.projects) {
                projects.value = resp.data.value.projects
                loaded.value = true
            }
        } catch {
            // Non-fatal: the sidebar simply shows no projects section.
        } finally {
            loading.value = false
        }
    }

    const createProject = async (fields: { name: string; description?: string; color?: string }) => {
        const resp: any = await useMyFetch('/projects', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(fields),
        })
        if (resp?.error?.value) throw resp.error.value
        await fetchProjects()
        return resp.data?.value as Project
    }

    const updateProject = async (id: string, fields: Partial<Pick<Project, 'name' | 'description' | 'color' | 'access'>>) => {
        const resp: any = await useMyFetch(`/projects/${id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(fields),
        })
        if (resp?.error?.value) throw resp.error.value
        await fetchProjects()
        return resp.data?.value as Project
    }

    const deleteProject = async (id: string) => {
        const resp: any = await useMyFetch(`/projects/${id}`, { method: 'DELETE' })
        if (resp?.error?.value) throw resp.error.value
        await fetchProjects()
    }

    // Move a single report into a project (or out, with projectId=null).
    // Uses the sentinel-aware report update endpoint ("" clears).
    const moveReport = async (reportId: string, projectId: string | null) => {
        const resp: any = await useMyFetch(`/reports/${reportId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ project_id: projectId ?? '' }),
        })
        if (resp?.error?.value) throw resp.error.value
        await fetchProjects() // refresh counts
        return resp.data?.value
    }

    return {
        projects,
        loaded,
        loading,
        fetchProjects,
        createProject,
        updateProject,
        deleteProject,
        moveReport,
    }
}
