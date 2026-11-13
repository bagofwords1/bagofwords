<template>
    <div class="flex justify-center ps-2 md:ps-4 text-sm">
        <div class="w-full max-w-4xl px-4 ps-0 py-8">
            <!-- Header -->
            <div class="mb-6">
                <h1 class="text-xl font-semibold text-ink">{{ $t('files.title') }}</h1>
                <p class="mt-1 text-ink-muted">{{ $t('files.subtitle') }}</p>
            </div>

            <!-- Source tabs, with search riding on the same rule (as /reports). -->
            <div class="border-b border-line mb-3">
                <nav class="-mb-px flex items-center gap-6">
                    <button
                        v-for="tab in tabs"
                        :key="tab.value"
                        type="button"
                        class="whitespace-nowrap border-b-2 py-2 px-1 text-sm transition-colors inline-flex items-center gap-1.5"
                        :class="source === tab.value
                            ? 'border-blue-500 text-blue-500'
                            : 'border-transparent text-ink-muted hover:border-line-strong hover:text-ink-2'"
                        :data-testid="`files-tab-${tab.value}`"
                        @click="source = tab.value"
                    >
                        {{ tab.label }}
                        <span
                            v-if="loaded"
                            class="nums rounded-full px-1.5 text-2xs"
                            :class="source === tab.value ? 'bg-blue-50 dark:bg-blue-950 text-blue-600 dark:text-blue-300' : 'bg-surface-muted text-ink-muted'"
                        >{{ tab.count }}</span>
                    </button>

                    <div class="ms-auto pb-1.5 w-40 sm:w-64">
                        <UInput
                            v-model="search"
                            icon="i-heroicons-magnifying-glass"
                            size="sm"
                            :placeholder="$t('files.search')"
                            data-testid="files-search"
                        />
                    </div>
                </nav>
            </div>

            <!-- List -->
            <div
                v-if="!loaded || shown.length"
                class="rounded-xl border border-line overflow-hidden bg-surface"
            >
                <!-- Selection strip: select-all and the bulk action in one bar. -->
                <div
                    v-if="loaded && deletableShown.length"
                    class="flex items-center gap-2 px-4 py-2 bg-surface-subtle text-xs text-ink-muted border-b border-line"
                >
                    <UCheckbox
                        :model-value="allShownSelected"
                        :indeterminate="someShownSelected && !allShownSelected"
                        :aria-label="$t('files.selectAll')"
                        data-testid="files-select-all"
                        @change="toggleAllShown"
                    />
                    <span v-if="selectedShown.length">{{ $t('files.selectedCount', { count: selectedShown.length }) }}</span>
                    <span v-else>{{ $t('files.selectAll') }}</span>
                    <UButton
                        v-if="selectedShown.length"
                        class="ms-auto"
                        color="red"
                        variant="ghost"
                        size="xs"
                        icon="i-heroicons-trash"
                        data-testid="files-delete-selected"
                        @click="askDelete(selectedShown)"
                    >
                        {{ $t('files.deleteSelected', { count: selectedShown.length }) }}
                    </UButton>
                </div>

                <!-- Loading skeleton -->
                <template v-if="!loaded">
                    <div v-for="i in 5" :key="i" class="flex items-center gap-3 px-4 py-3 border-b border-line last:border-b-0">
                        <div class="w-9 h-9 rounded-lg bg-surface-muted bow-shimmer shrink-0" />
                        <div class="flex-1 space-y-2">
                            <div class="h-3 w-1/3 rounded bg-surface-muted bow-shimmer" />
                            <div class="h-2.5 w-1/5 rounded bg-surface-muted bow-shimmer" />
                        </div>
                    </div>
                </template>

                <ul v-else class="divide-y divide-gray-100 dark:divide-gray-800">
                    <li
                        v-for="file in shown"
                        :key="file.id"
                        :data-testid="`file-row-${file.id}`"
                        class="group flex items-center gap-3 px-4 py-3 hover:bg-surface-subtle transition-colors"
                    >
                        <UCheckbox
                            v-if="file.can_delete"
                            :model-value="selected.has(file.id)"
                            :aria-label="file.filename"
                            class="shrink-0 transition-opacity"
                            :class="selected.size ? '' : 'opacity-0 group-hover:opacity-100 focus-within:opacity-100'"
                            @change="toggle(file.id)"
                        />
                        <span v-else class="w-4 shrink-0" />

                        <!-- Type avatar -->
                        <span class="flex items-center justify-center w-9 h-9 rounded-lg bg-surface-muted shrink-0">
                            <UIcon :name="fileIcon(file)" class="w-5 h-5 text-ink-muted" />
                        </span>

                        <!-- Name + meta -->
                        <div class="min-w-0 flex-1">
                            <div class="flex items-center gap-2 min-w-0">
                                <span v-text-dir class="truncate font-medium text-ink" :title="file.filename">{{ file.filename }}</span>
                                <span
                                    class="inline-flex shrink-0 items-center gap-1 rounded-full border border-line px-2 py-0.5 text-2xs text-ink-muted"
                                    :data-testid="`file-source-${file.id}`"
                                >
                                    <UIcon :name="isAgent(file) ? 'i-heroicons-sparkles' : 'i-heroicons-arrow-up-tray'" class="w-3 h-3" />
                                    {{ isAgent(file) ? $t('files.sourceAgent') : $t('files.sourceUpload') }}
                                </span>
                                <UTooltip v-if="!file.can_delete" :text="$t('files.inUse')">
                                    <UIcon name="i-heroicons-lock-closed" class="w-3.5 h-3.5 text-ink-muted" />
                                </UTooltip>
                            </div>
                            <div class="mt-0.5 flex items-center gap-2 text-xs text-ink-muted">
                                <span>{{ typeLabel(file) }}</span>
                                <template v-if="metaLabel(file)">
                                    <span aria-hidden="true">·</span>
                                    <UTooltip :text="metaTooltip(file)">
                                        <span>{{ metaLabel(file) }}</span>
                                    </UTooltip>
                                </template>
                            </div>
                        </div>

                        <!-- Date -->
                        <UTooltip :text="formatDate(file.created_at)" class="shrink-0 hidden sm:block">
                            <span class="text-xs text-ink-muted nums">{{ relativeTime(file.created_at) }}</span>
                        </UTooltip>

                        <!-- Actions -->
                        <div class="shrink-0 flex items-center gap-0.5 sm:opacity-0 sm:group-hover:opacity-100 sm:focus-within:opacity-100 transition-opacity">
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
                        </div>
                    </li>
                </ul>
            </div>

            <!-- Empty state -->
            <div v-else class="rounded-xl border border-dashed border-line py-16 text-center" data-testid="files-empty">
                <UIcon name="i-heroicons-document" class="w-8 h-8 text-ink-subtle" />
                <p class="mt-2 font-medium text-ink-2">{{ files.length ? $t('files.empty') : $t('files.emptyNone') }}</p>
                <p v-if="!files.length" class="mt-1 text-xs text-ink-muted">{{ $t('files.emptyNoneHint') }}</p>
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
const { t } = useI18n()
const toast = useToast()
const { relativeTime } = useRelativeTime()
const _df = useFormatDate()

definePageMeta({ auth: true })

type Source = 'all' | 'uploads' | 'agent'

const files = ref<any[]>([])
const loaded = ref(false)
const search = ref('')
const source = ref<Source>('all')
const selected = ref<Set<string>>(new Set())
const confirmOpen = ref(false)
const pendingDelete = ref<string[]>([])
const deleting = ref(false)

// Anything other than a user upload was put there by the agent: tool output
// (an API response, a written CSV) or a cached copy of a connector file.
const isAgent = (f: any) => (f?.source_kind || 'upload') !== 'upload'

const tabs = computed(() => [
    { value: 'all' as Source, label: t('files.filterAll'), count: files.value.length },
    { value: 'uploads' as Source, label: t('files.filterUploads'), count: files.value.filter((f) => !isAgent(f)).length },
    { value: 'agent' as Source, label: t('files.filterAgent'), count: files.value.filter(isAgent).length },
])

const shown = computed(() => {
    const q = search.value.trim().toLowerCase()
    return files.value.filter((f) => {
        if (source.value === 'uploads' && isAgent(f)) return false
        if (source.value === 'agent' && !isAgent(f)) return false
        return !q || String(f.filename || '').toLowerCase().includes(q)
    })
})
const deletableShown = computed(() => shown.value.filter((f) => f.can_delete))
// Only rows on screen count as selected: a filter or search never leaves a
// hidden selection behind for the bulk delete to sweep up.
const selectedShown = computed(() => deletableShown.value.filter((f) => selected.value.has(f.id)).map((f) => f.id))
const allShownSelected = computed(() => deletableShown.value.length > 0 && selectedShown.value.length === deletableShown.value.length)
const someShownSelected = computed(() => selectedShown.value.length > 0)

watch([source, search], () => {
    const visible = new Set(shown.value.map((f) => f.id))
    selected.value = new Set([...selected.value].filter((id) => visible.has(id)))
})

function toggle(id: string) {
    const next = new Set(selected.value)
    next.has(id) ? next.delete(id) : next.add(id)
    selected.value = next
}

function toggleAllShown() {
    selected.value = allShownSelected.value ? new Set() : new Set(deletableShown.value.map((f) => f.id))
}

function extension(f: any) {
    const name = String(f?.filename || '')
    const i = name.lastIndexOf('.')
    return i > 0 ? name.slice(i + 1).toLowerCase() : ''
}

function fileIcon(f: any) {
    const ext = extension(f)
    const ct = String(f?.content_type || '')
    if (['csv', 'xlsx', 'xls', 'tsv'].includes(ext)) return 'i-heroicons-table-cells'
    if (ct.startsWith('image/')) return 'i-heroicons-photo'
    if (['json', 'xml', 'html', 'sql', 'py', 'js', 'yaml', 'yml'].includes(ext)) return 'i-heroicons-code-bracket'
    if (ext === 'pdf') return 'i-heroicons-document-text'
    return 'i-heroicons-document'
}

function typeLabel(f: any) {
    return extension(f).toUpperCase() || String(f?.content_type || '').split('/').pop()?.toUpperCase() || ''
}

function metaLabel(f: any) {
    const fields = (f.schemas || []).reduce((n: number, s: any) => n + Object.keys(s?.schema?.fields || {}).length, 0)
    if (fields) return t(fields === 1 ? 'files.metadataFieldsOne' : 'files.metadataFieldsMany', { count: fields })
    const tags = (f.tags || []).length
    if (tags) return t(tags === 1 ? 'files.metadataTagsOne' : 'files.metadataTagsMany', { count: tags })
    return ''
}

function metaTooltip(f: any) {
    const fields = (f.schemas || []).flatMap((s: any) => Object.keys(s?.schema?.fields || {}))
    return (fields.length ? fields : (f.tags || []).map((tag: any) => tag.key)).join(', ')
}

function formatDate(iso: string) {
    return iso ? _df.format(iso, { dateStyle: 'medium', timeStyle: 'short' } as any) : ''
}

async function getFiles() {
    const { data } = await useMyFetch('/api/files', { method: 'GET' })
    files.value = ((data.value as any[]) || []).slice()
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
        selected.value = new Set([...selected.value].filter((id) => !deleted.has(id)))
        const failed = (data.value.failed || []).length
        if (deleted.size) toast.add({ title: t(deleted.size === 1 ? 'files.toastDeletedOne' : 'files.toastDeletedMany', { count: deleted.size }), color: 'green' })
        if (failed) toast.add({ title: t(failed === 1 ? 'files.toastPartialOne' : 'files.toastPartialMany', { count: failed }), color: 'orange' })
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
