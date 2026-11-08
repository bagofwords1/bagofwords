<template>
    <div class="flex ps-2 md:ps-4 text-sm">
        <div class="w-full md:w-3/4 px-4 ps-0 py-4">
            <div>
                <h1 class="text-lg font-semibold text-ink">
                    {{ $t('files.title') }}
                </h1>
                <p class="mt-2 text-ink-muted">{{ $t('files.subtitle') }}</p>
            </div>

            <div class="mt-6 flex flex-wrap items-center gap-2">
                <UInput
                    v-model="search"
                    icon="i-heroicons-magnifying-glass"
                    size="sm"
                    class="w-full sm:w-72"
                    :placeholder="$t('files.search')"
                    data-testid="files-search"
                />
                <USelectMenu
                    v-model="source"
                    :options="sourceOptions"
                    value-attribute="value"
                    option-attribute="label"
                    size="sm"
                    class="w-40"
                    data-testid="files-source"
                />
                <span class="text-xs text-ink-muted nums">{{ $t('files.showingCount', { shown: shown.length, total: files.length }) }}</span>
                <div class="ms-auto">
                    <UButton
                        v-if="selected.size > 0"
                        color="red"
                        size="sm"
                        icon="i-heroicons-trash"
                        data-testid="files-delete-selected"
                        @click="askDelete([...selected])"
                    >
                        {{ $t('files.deleteSelected', { count: selected.size }) }}
                    </UButton>
                </div>
            </div>

            <div class="bg-surface rounded-lg shadow mt-4 overflow-x-auto">
                <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
                    <thead class="bg-surface-subtle">
                        <tr>
                            <th class="ps-4 py-3 w-8">
                                <UCheckbox
                                    :model-value="allShownSelected"
                                    :indeterminate="someShownSelected && !allShownSelected"
                                    :disabled="deletableShown.length === 0"
                                    :aria-label="$t('files.selectAll')"
                                    data-testid="files-select-all"
                                    @change="toggleAllShown"
                                />
                            </th>
                            <th class="px-4 py-3 text-start text-xs font-medium text-ink-muted uppercase tracking-wider">{{ $t('files.file') }}</th>
                            <th class="px-4 py-3 text-start text-xs font-medium text-ink-muted uppercase tracking-wider">{{ $t('files.metadata') }}</th>
                            <th class="px-4 py-3 text-start text-xs font-medium text-ink-muted uppercase tracking-wider">{{ $t('files.createdAt') }}</th>
                            <th class="px-4 py-3 text-end text-xs font-medium text-ink-muted uppercase tracking-wider">{{ $t('files.actions') }}</th>
                        </tr>
                    </thead>

                    <tbody class="divide-y divide-gray-200 dark:divide-gray-700">
                        <tr v-for="file in shown" :key="file.id" :data-testid="`file-row-${file.id}`">
                            <td class="ps-4 py-3">
                                <UCheckbox
                                    v-if="file.can_delete"
                                    :model-value="selected.has(file.id)"
                                    :aria-label="file.filename"
                                    @change="toggle(file.id)"
                                />
                            </td>
                            <td class="px-4 py-3">
                                <div class="flex items-center gap-2 min-w-0">
                                    <UIcon name="heroicons-document-text" class="w-5 h-5 shrink-0 text-ink-muted" />
                                    <span class="truncate text-ink">{{ file.filename }}</span>
                                    <UBadge
                                        v-if="isGenerated(file)"
                                        color="gray"
                                        variant="soft"
                                        size="xs"
                                    >{{ $t('files.sourceGenerated') }}</UBadge>
                                </div>
                            </td>
                            <td class="px-4 py-3 whitespace-nowrap text-ink-2">
                                <div v-if="file.schemas?.length > 0">
                                    <div v-for="schema in file.schemas" :key="schema.id">
                                        <UTooltip :text="Object.keys(schema.schema.fields).join(', ')">
                                            <div class="flex items-center">
                                                <Icon name="heroicons-view-columns" class="w-5 h-5 text-ink-muted me-2" />
                                                {{ $t(Object.keys(schema.schema.fields).length === 1 ? 'files.metadataFieldsOne' : 'files.metadataFieldsMany', { count: Object.keys(schema.schema.fields).length }) }}
                                            </div>
                                        </UTooltip>
                                    </div>
                                </div>
                                <div v-else-if="file.tags?.length > 0">
                                    <UTooltip :text="file.tags.map((tag: any) => tag.key).join(', ')">
                                        <div class="flex items-center">
                                            <Icon name="heroicons-view-columns" class="w-5 h-5 text-ink-muted me-2" />
                                            {{ $t(file.tags.length === 1 ? 'files.metadataTagsOne' : 'files.metadataTagsMany', { count: file.tags.length }) }}
                                        </div>
                                    </UTooltip>
                                </div>
                                <div v-else class="text-ink-muted">
                                    {{ $t('files.noMetadata') }}
                                </div>
                            </td>
                            <td class="px-4 py-3 whitespace-nowrap text-ink-2 nums">{{ formatDate(file.created_at) }}</td>
                            <td class="px-4 py-3 whitespace-nowrap text-end">
                                <UTooltip :text="$t('files.download')">
                                    <UButton
                                        color="gray"
                                        variant="ghost"
                                        size="xs"
                                        icon="i-heroicons-arrow-down-tray"
                                        :aria-label="$t('files.download')"
                                        @click="downloadFile(file)"
                                    />
                                </UTooltip>
                                <UTooltip v-if="file.can_delete" :text="$t('files.delete')">
                                    <UButton
                                        color="gray"
                                        variant="ghost"
                                        size="xs"
                                        icon="i-heroicons-trash"
                                        :aria-label="$t('files.delete')"
                                        :data-testid="`file-delete-${file.id}`"
                                        @click="askDelete([file.id])"
                                    />
                                </UTooltip>
                            </td>
                        </tr>
                        <tr v-if="loaded && shown.length === 0">
                            <td colspan="5" class="px-4 py-8 text-center text-ink-muted">{{ $t('files.empty') }}</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>

        <UModal v-model="confirmOpen" :ui="{ width: 'sm:max-w-md' }">
            <div class="p-5" data-testid="files-delete-confirm">
                <h3 class="text-base font-semibold text-ink">
                    {{ $t(pendingDelete.length === 1 ? 'files.confirmTitleOne' : 'files.confirmTitleMany', { count: pendingDelete.length }) }}
                </h3>
                <p class="mt-2 text-sm text-ink-2">{{ $t('files.confirmBody') }}</p>
                <div class="mt-5 flex justify-end gap-2">
                    <UButton color="gray" variant="ghost" :disabled="deleting" @click="confirmOpen = false">{{ $t('common.cancel') }}</UButton>
                    <UButton color="red" :loading="deleting" data-testid="files-delete-confirm-button" @click="runDelete">{{ $t('common.delete') }}</UButton>
                </div>
            </div>
        </UModal>
    </div>
</template>

<script setup lang="ts">
const { t, locale } = useI18n()
const toast = useToast()

definePageMeta({ auth: true })

const files = ref<any[]>([])
const loaded = ref(false)
const search = ref('')
const source = ref<'all' | 'uploads' | 'generated'>('all')
const selected = ref<Set<string>>(new Set())
const confirmOpen = ref(false)
const pendingDelete = ref<string[]>([])
const deleting = ref(false)

const sourceOptions = computed(() => [
    { value: 'all', label: t('files.filterAll') },
    { value: 'uploads', label: t('files.filterUploads') },
    { value: 'generated', label: t('files.filterGenerated') },
])

// Anything other than a user upload was put there by the agent: tool output
// (an API response, a written CSV) or a cached copy of a connector file.
const isGenerated = (f: any) => (f?.source_kind || 'upload') !== 'upload'

const shown = computed(() => {
    const q = search.value.trim().toLowerCase()
    return files.value.filter((f) => {
        if (source.value === 'uploads' && isGenerated(f)) return false
        if (source.value === 'generated' && !isGenerated(f)) return false
        return !q || String(f.filename || '').toLowerCase().includes(q)
    })
})
const deletableShown = computed(() => shown.value.filter((f) => f.can_delete))
const allShownSelected = computed(() => deletableShown.value.length > 0 && deletableShown.value.every((f) => selected.value.has(f.id)))
const someShownSelected = computed(() => deletableShown.value.some((f) => selected.value.has(f.id)))

function toggle(id: string) {
    const next = new Set(selected.value)
    next.has(id) ? next.delete(id) : next.add(id)
    selected.value = next
}

function toggleAllShown() {
    const next = new Set(selected.value)
    if (allShownSelected.value) deletableShown.value.forEach((f) => next.delete(f.id))
    else deletableShown.value.forEach((f) => next.add(f.id))
    selected.value = next
}

function formatDate(value: string | null | undefined) {
    if (!value) return ''
    // The API sends naive UTC timestamps.
    const d = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`)
    if (Number.isNaN(d.getTime())) return value
    return new Intl.DateTimeFormat(locale.value, { dateStyle: 'medium', timeStyle: 'short' }).format(d)
}

async function getFiles() {
    const { data } = await useMyFetch('/api/files', { method: 'GET' })
    const rows = (data.value as any[]) || []
    files.value = [...rows].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))
    loaded.value = true
}

async function downloadFile(file: any) {
    try {
        const { data, error } = await useMyFetch<any>(`/api/files/${file.id}/content`, { method: 'GET', responseType: 'blob' as any })
        if (error.value || !data.value) throw error.value || new Error('empty')
        const url = URL.createObjectURL(data.value as Blob)
        const a = document.createElement('a')
        a.href = url
        a.download = file.filename || 'file'
        document.body.appendChild(a)
        a.click()
        a.remove()
        URL.revokeObjectURL(url)
    } catch {
        toast.add({ title: t('files.toastDownloadError'), color: 'red' })
    }
}

function askDelete(ids: string[]) {
    pendingDelete.value = ids
    confirmOpen.value = true
}

async function runDelete() {
    deleting.value = true
    try {
        const { data, error } = await useMyFetch<any>('/api/files/delete', {
            method: 'POST',
            body: { file_ids: pendingDelete.value },
        })
        if (error.value || !data.value) throw error.value || new Error('delete failed')
        const deleted = new Set<string>(data.value.deleted || [])
        files.value = files.value.filter((f) => !deleted.has(f.id))
        const next = new Set(selected.value)
        deleted.forEach((id) => next.delete(id))
        selected.value = next
        const failed = (data.value.failed || []).length
        if (deleted.size) toast.add({ title: t(deleted.size === 1 ? 'files.toastDeletedOne' : 'files.toastDeletedMany', { count: deleted.size }), color: 'green' })
        if (failed) toast.add({ title: t('files.toastPartial', { count: failed }), color: 'orange' })
        confirmOpen.value = false
    } catch {
        toast.add({ title: t('files.toastError'), color: 'red' })
    } finally {
        deleting.value = false
    }
}

onMounted(() => {
    getFiles()
})
</script>
