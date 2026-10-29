<template>
    <!-- Settings › Access: who can edit and who can view, resolved to names.
         Read only; changes are made in Share. -->
    <div class="space-y-6" data-testid="settings-access">
        <div v-if="loading" class="flex justify-center py-10"><Spinner class="w-5 h-5 text-gray-400" /></div>
        <p v-else-if="failed" class="text-sm text-gray-500">{{ $t('dashboardSettings.access.loadFailed') }}</p>
        <template v-else>
            <!-- Can edit -->
            <section data-testid="access-editors">
                <h3 class="text-sm font-medium text-gray-900 dark:text-white mb-2">
                    {{ $t('dashboardSettings.access.canEdit') }}
                    <span class="text-gray-400 font-normal tabular-nums">· {{ editorCount }}</span>
                </h3>
                <ul class="divide-y divide-gray-100 dark:divide-gray-800 rounded-lg border border-gray-200 dark:border-gray-700">
                    <li v-for="row in editRows" :key="row.key" class="flex items-center gap-3 px-3 py-2" data-testid="access-row">
                        <div class="w-7 h-7 shrink-0 rounded-full flex items-center justify-center text-xs font-medium"
                            :class="row.kind === 'user' ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300' : 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'">
                            <Icon v-if="row.kind !== 'user'" :name="row.icon" class="w-4 h-4" />
                            <template v-else>{{ initial(row.name) }}</template>
                        </div>
                        <div class="min-w-0 flex-1">
                            <div class="text-sm text-gray-900 dark:text-gray-100 truncate">{{ row.name }}</div>
                            <div v-if="row.sub" class="text-xs text-gray-500 dark:text-gray-400 truncate">{{ row.sub }}</div>
                        </div>
                        <span class="text-xs text-gray-500 dark:text-gray-400 shrink-0">{{ row.role }}</span>
                    </li>
                </ul>
            </section>

            <!-- Can view -->
            <section data-testid="access-viewers">
                <h3 class="text-sm font-medium text-gray-900 dark:text-white mb-2">{{ $t('dashboardSettings.access.canView') }}</h3>
                <div v-if="!sharing.published" class="text-sm text-gray-500 dark:text-gray-400 rounded-lg border border-dashed border-gray-200 dark:border-gray-700 px-3 py-3"
                    data-testid="access-unpublished">
                    {{ $t('dashboardSettings.access.notPublished') }}
                </div>
                <div v-else-if="sharing.visibility === 'internal' || sharing.visibility === 'public'"
                    class="flex items-center gap-3 rounded-lg border border-gray-200 dark:border-gray-700 px-3 py-2.5" data-testid="access-general">
                    <div class="w-7 h-7 shrink-0 rounded-full flex items-center justify-center bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-400">
                        <Icon :name="sharing.visibility === 'public' ? 'heroicons:globe-alt' : 'heroicons:building-office'" class="w-4 h-4" />
                    </div>
                    <div class="min-w-0 flex-1 text-sm text-gray-900 dark:text-gray-100">
                        {{ sharing.visibility === 'public' ? $t('dashboardSettings.access.anyoneWithLink') : $t('dashboardSettings.access.everyoneInOrg') }}
                    </div>
                </div>
                <template v-else>
                    <ul v-if="viewRows.length" class="divide-y divide-gray-100 dark:divide-gray-800 rounded-lg border border-gray-200 dark:border-gray-700">
                        <li v-for="row in viewRows" :key="row.key" class="flex items-center gap-3 px-3 py-2" data-testid="access-row">
                            <div class="w-7 h-7 shrink-0 rounded-full flex items-center justify-center text-xs font-medium"
                                :class="row.kind === 'user' ? 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300' : 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'">
                                <Icon v-if="row.kind !== 'user'" :name="row.icon" class="w-4 h-4" />
                                <template v-else>{{ initial(row.name) }}</template>
                            </div>
                            <div class="min-w-0 flex-1">
                                <div class="text-sm text-gray-900 dark:text-gray-100 truncate">{{ row.name }}</div>
                                <div v-if="row.sub" class="text-xs text-gray-500 dark:text-gray-400 truncate">{{ row.sub }}</div>
                            </div>
                        </li>
                    </ul>
                    <p v-else class="text-sm text-gray-500 dark:text-gray-400">{{ $t('dashboardSettings.access.onlyEditors') }}</p>
                </template>
                <p class="text-xs text-gray-400 mt-2">{{ $t('dashboardSettings.access.editorsAlsoView') }}</p>
            </section>

            <p class="text-xs text-gray-500 dark:text-gray-400 flex items-center gap-1.5">
                <Icon name="heroicons:information-circle" class="w-4 h-4 shrink-0" />
                {{ $t('dashboardSettings.access.changeInShare') }}
            </p>
        </template>
    </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'

const props = defineProps<{ dashboardId: string; active?: boolean }>()
const { t } = useI18n()

type Row = { key: string; kind: 'user' | 'group' | 'project' | 'org'; name: string; sub?: string; role?: string; icon: string }

const loading = ref(true)
const failed = ref(false)
const sharing = ref<any>({ visibility: 'none', published: false, user_ids: [], group_ids: [], editor_user_ids: [], editor_group_ids: [] })
const members = ref<Record<string, { name: string; email: string }>>({})
const groups = ref<Record<string, { name: string; count: number }>>({})
const project = ref<any | null>(null)
const projectMembers = ref<any[]>([])

const initial = (s: string) => (s || '?').trim().charAt(0).toUpperCase()
const person = (id: string) => members.value[id] || { name: t('dashboardCollab.history.someone'), email: '' }
const groupRow = (id: string, role?: string): Row => {
    const g = groups.value[id]
    return { key: `g:${id}`, kind: 'group', icon: 'heroicons:user-group', name: g?.name || t('dashboardSettings.access.aGroup'),
             sub: g ? t('dashboardSettings.access.members', { n: g.count }, g.count) : undefined, role }
}

const editRows = computed<Row[]>(() => {
    const s = sharing.value
    const rows: Row[] = []
    const seen = new Set<string>()
    if (s.owner_id) {
        const p = person(s.owner_id)
        rows.push({ key: `u:${s.owner_id}`, kind: 'user', icon: '', name: p.name || p.email, sub: p.name ? p.email : undefined, role: t('dashboardCollab.share.owner') })
        seen.add(s.owner_id)
    }
    for (const id of s.editor_user_ids || []) {
        if (seen.has(id)) continue
        seen.add(id)
        const p = person(id)
        rows.push({ key: `u:${id}`, kind: 'user', icon: '', name: p.name || p.email, sub: p.name ? p.email : undefined, role: t('dashboardCollab.share.canEdit') })
    }
    for (const id of s.editor_group_ids || []) rows.push(groupRow(id, t('dashboardCollab.share.canEdit')))
    // Project collaborators edit its dashboards (collab01).
    if (s.project_id) {
        if (project.value?.access === 'org') {
            rows.push({ key: 'project-org', kind: 'org', icon: 'heroicons:building-office', role: t('dashboardCollab.share.canEdit'),
                        name: t('dashboardSettings.access.everyoneInOrg'), sub: t('dashboardSettings.access.viaProject', { name: project.value?.name || '' }) })
        } else if (projectMembers.value.length) {
            for (const m of projectMembers.value) {
                if (seen.has(m.user_id)) continue
                seen.add(m.user_id)
                rows.push({ key: `p:${m.user_id}`, kind: 'user', icon: '', name: m.user_name || m.user_email,
                            sub: t('dashboardSettings.access.viaProject', { name: project.value?.name || '' }), role: t('dashboardCollab.share.canEdit') })
            }
        } else {
            rows.push({ key: 'project', kind: 'project', icon: 'heroicons:folder', role: t('dashboardCollab.share.canEdit'),
                        name: project.value?.name ? t('dashboardSettings.access.projectMembers', { name: project.value.name }) : t('dashboardSettings.access.projectMembersUnnamed') })
        }
    }
    return rows
})
const editorCount = computed(() => editRows.value.length)

const viewRows = computed<Row[]>(() => {
    const s = sharing.value
    const editors = new Set([s.owner_id, ...(s.editor_user_ids || [])])
    const rows: Row[] = []
    for (const id of s.user_ids || []) {
        if (editors.has(id)) continue
        const p = person(id)
        rows.push({ key: `u:${id}`, kind: 'user', icon: '', name: p.name || p.email, sub: p.name ? p.email : undefined })
    }
    const editorGroups = new Set(s.editor_group_ids || [])
    for (const id of s.group_ids || []) if (!editorGroups.has(id)) rows.push(groupRow(id))
    return rows
})

async function load() {
    loading.value = true
    failed.value = false
    try {
        const [sh, mem, grp] = await Promise.all([
            useMyFetch(`/artifacts/${props.dashboardId}/sharing`),
            useMyFetch('/organization/members'),
            useMyFetch('/organization/groups'),
        ])
        if (sh.error.value || !sh.data.value) { failed.value = true; return }
        sharing.value = sh.data.value
        members.value = Object.fromEntries(((mem.data.value as any[]) || []).map((u: any) => [u.id, { name: u.name || '', email: u.email || '' }]))
        groups.value = Object.fromEntries(((grp.data.value as any[]) || []).map((g: any) => [g.id, { name: g.name || '', count: g.member_count || 0 }]))
        project.value = null
        projectMembers.value = []
        const pid = (sh.data.value as any).project_id
        if (pid) {
            // Either may be refused to someone who can't open the project.
            const [pr, pm] = await Promise.all([useMyFetch(`/projects/${pid}`), useMyFetch(`/projects/${pid}/members`)])
            if (!pr.error.value) project.value = pr.data.value
            if (!pm.error.value && Array.isArray(pm.data.value)) projectMembers.value = pm.data.value as any[]
        }
    } catch {
        failed.value = true
    } finally {
        loading.value = false
    }
}
// Fresh each time the tab is shown: Share may have changed it meanwhile.
watch(() => props.active, (on) => { if (on) load() }, { immediate: true })
</script>
