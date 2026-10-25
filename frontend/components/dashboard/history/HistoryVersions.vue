<template>
    <!-- Every version of the dashboard, newest first, in one list: who made
         it and when, its name, whether viewers see it (Live), when it was
         last published. Preview any; Restore one published before (instant:
         its data was kept); Publish any other (its queries run again). -->
    <HistoryEmpty v-if="!rows.length" icon="heroicons:clock" :text="$t('dashboardCollab.history.noVersions')" />
    <ul v-else class="divide-y divide-gray-100 dark:divide-gray-800" data-testid="history-versions">
        <li v-for="v in rows" :key="v.id" class="py-3" data-testid="history-version" :data-version="v.number">
            <div class="flex items-start justify-between gap-3">
                <div class="min-w-0 flex-1">
                    <div class="flex flex-wrap items-center gap-x-1.5 gap-y-1 text-sm">
                        <span class="font-medium text-gray-900 dark:text-gray-100 tabular-nums">{{ $t('dashboardCollab.history.versionLabel', { n: v.number }) }}</span>
                        <span v-if="v.name && naming !== v.id" class="text-gray-900 dark:text-gray-100 truncate" data-testid="history-version-name">“{{ v.name }}”</span>
                        <UBadge v-if="v.live" color="green" variant="solid" size="xs" data-testid="history-version-live">
                            {{ $t('dashboardCollab.versions.live') }}
                        </UBadge>
                    </div>
                    <div class="flex flex-wrap items-center gap-x-1.5 text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                        <span class="truncate">{{ v.author?.name || $t('dashboardCollab.history.someone') }}</span>
                        <template v-if="v.created_at">
                            <span aria-hidden="true">·</span>
                            <span class="whitespace-nowrap">{{ relativeTime(v.created_at) }}</span>
                        </template>
                        <template v-if="v.published_at">
                            <span aria-hidden="true">·</span>
                            <span class="whitespace-nowrap" data-testid="history-version-published">
                                {{ v.live ? $t('dashboardCollab.versions.liveSince', { when: relativeTime(v.published_at) }) : $t('dashboardCollab.versions.publishedAt', { when: relativeTime(v.published_at) }) }}<template v-if="v.publish_count > 1">{{ $t('dashboardCollab.versions.publishedTimes', { n: v.publish_count }) }}</template>
                            </span>
                        </template>
                    </div>
                </div>
                <div class="flex items-center gap-1 shrink-0">
                    <UButton size="xs" color="gray" variant="ghost" icon="i-heroicons-eye"
                        data-testid="history-version-preview" @click="emit('preview', v.number)">
                        {{ $t('dashboardCollab.history.preview') }}
                    </UButton>
                    <UDropdown v-if="menu(v).length" :items="[menu(v)]" :popper="{ placement: 'bottom-end' }">
                        <UButton size="xs" color="gray" variant="ghost" icon="i-heroicons-ellipsis-horizontal"
                            :aria-label="$t('dashboardCollab.versions.more')" data-testid="history-version-menu" />
                    </UDropdown>
                </div>
            </div>

            <!-- Name in place -->
            <form v-if="naming === v.id" class="mt-2 flex items-center gap-2" data-testid="history-version-name-form" @submit.prevent="saveName(v)">
                <UInput v-model="nameDraft" size="sm" class="flex-1" :maxlength="120" autofocus
                    :placeholder="$t('dashboardCollab.versions.namePlaceholder')" @keydown.escape.prevent="naming = null" />
                <UButton size="xs" color="gray" variant="ghost" :disabled="busy === v.id" @click="naming = null">{{ $t('common.cancel') }}</UButton>
                <UButton size="xs" type="submit" :loading="busy === v.id" data-testid="history-version-name-save">{{ $t('common.save') }}</UButton>
            </form>

            <!-- Confirm in place: Restore and Publish change what viewers see. -->
            <div v-if="confirming?.id === v.id" class="mt-2 p-3 rounded-lg border border-amber-200 dark:border-amber-900 bg-amber-50 dark:bg-amber-950/40 space-y-2"
                data-testid="history-version-confirm">
                <p class="text-sm font-medium text-gray-900 dark:text-gray-100">
                    {{ confirming.kind === 'restore' ? $t('dashboardCollab.versions.restoreTitle', { n: v.number }) : $t('dashboardCollab.versions.publishTitle', { n: v.number }) }}
                </p>
                <p class="text-xs text-gray-600 dark:text-gray-400">
                    {{ confirming.kind === 'restore' ? $t('dashboardCollab.versions.restoreBody') : $t('dashboardCollab.versions.publishBody') }}
                </p>
                <div class="flex flex-wrap justify-end gap-2">
                    <UButton size="xs" color="gray" variant="ghost" :disabled="!!busy" @click="confirming = null">{{ $t('common.cancel') }}</UButton>
                    <UButton size="xs" color="amber" :icon="confirming.kind === 'restore' ? 'i-heroicons-arrow-uturn-left' : 'i-heroicons-rocket-launch'"
                        :loading="busy === v.id" data-testid="history-version-confirm-go" @click="go(v)">
                        {{ confirming.kind === 'restore' ? $t('dashboardCollab.versions.restore') : $t('dashboardCollab.versions.publish') }}
                    </UButton>
                </div>
            </div>
        </li>
    </ul>
</template>

<script setup lang="ts">
import HistoryEmpty from './HistoryEmpty.vue'

const props = defineProps<{
    versions: any[]
    dashboardId: string
    // Name (editors), Restore and Publish (whoever may publish).
    canEdit?: boolean
    canPublish?: boolean
}>()
const emit = defineEmits<{
    (e: 'preview', number: number): void
    (e: 'changed'): void       // a name was saved
    (e: 'rolled-back'): void   // what viewers see changed at once
    (e: 'publishing'): void    // a publish started; its queries run
}>()
const { t } = useI18n()
const toast = useToast()
const { relativeTime } = useRelativeTime()
const { getErrorMessage } = useErrorMessage()

const rows = computed(() => [...(props.versions || [])].sort((a, b) => (b.number ?? 0) - (a.number ?? 0)))
const naming = ref<string | null>(null)
const nameDraft = ref('')
const confirming = ref<{ id: string; kind: 'restore' | 'publish' } | null>(null)
const busy = ref<string | null>(null)

function menu(v: any) {
    const items: any[] = []
    if (props.canEdit) {
        items.push({ label: v.name ? t('dashboardCollab.versions.rename') : t('dashboardCollab.versions.name'), icon: 'i-heroicons-tag',
            click: () => { confirming.value = null; nameDraft.value = v.name || ''; naming.value = v.id } })
    }
    if (props.canPublish && !v.live) {
        if (v.restore_release_id) {
            items.push({ label: t('dashboardCollab.versions.restore'), icon: 'i-heroicons-arrow-uturn-left',
                click: () => { naming.value = null; confirming.value = { id: v.id, kind: 'restore' } } })
        } else {
            items.push({ label: t('dashboardCollab.versions.publishThis'), icon: 'i-heroicons-rocket-launch',
                click: () => { naming.value = null; confirming.value = { id: v.id, kind: 'publish' } } })
        }
    }
    return items
}

async function saveName(v: any) {
    busy.value = v.id
    try {
        const res = await useMyFetch(`/artifacts/${props.dashboardId}/versions/${v.id}/name`, { method: 'PUT', body: { name: nameDraft.value } })
        if (res.error.value) throw res.error.value
        naming.value = null
        emit('changed')
    } catch (e) {
        toast.add({ title: t('dashboardCollab.versions.nameFailed'), description: getErrorMessage(e), color: 'red' })
    } finally {
        busy.value = null
    }
}

async function go(v: any) {
    const kind = confirming.value?.kind
    busy.value = v.id
    try {
        if (kind === 'restore') {
            const res = await useMyFetch(`/artifacts/${props.dashboardId}/restore`, { method: 'POST', body: { release_id: v.restore_release_id } })
            if (res.error.value) throw res.error.value
            toast.add({ title: t('dashboardCollab.versions.restoreDone', { n: v.number }), color: 'green' })
            emit('rolled-back')
        } else {
            // A deliberate pick from the list: no "published since" question.
            const res = await useMyFetch(`/artifacts/${props.dashboardId}/publish`, { method: 'POST', body: { version_id: v.id, force: true } })
            if (res.error.value) throw res.error.value
            toast.add({ title: t('dashboardCollab.versions.publishStarted', { n: v.number }), color: 'green' })
            emit('publishing')
        }
        confirming.value = null
    } catch (e) {
        toast.add({ title: kind === 'restore' ? t('dashboardCollab.versions.restoreFailed') : t('dashboardCollab.versions.publishFailed'),
            description: getErrorMessage(e), color: 'red' })
    } finally {
        busy.value = null
    }
}
</script>
