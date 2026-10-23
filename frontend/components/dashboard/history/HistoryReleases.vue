<template>
    <!-- What readers saw, newest first: the live one, the kept ones (each can
         become live again: Roll back), and failed attempts, greyed. -->
    <HistoryEmpty v-if="!rows.length" icon="heroicons:rocket-launch" :text="$t('dashboardCollab.history.noReleases')" />
    <ul v-else class="divide-y divide-gray-100 dark:divide-gray-800" data-testid="history-releases">
        <li v-for="r in rows" :key="r.id" class="py-2.5" data-testid="history-release"
            :class="r.status === 'failed' ? 'opacity-60' : ''">
            <div class="flex items-center justify-between gap-3">
                <div class="min-w-0 flex-1 space-y-0.5">
                    <div class="flex flex-wrap items-center gap-1.5">
                        <UBadge v-if="r.live" color="green" variant="solid" size="xs" data-testid="history-release-live">
                            {{ $t('dashboardCollab.history.live') }}
                        </UBadge>
                        <UBadge v-else-if="r.status === 'failed'" color="red" variant="soft" size="xs">
                            {{ $t('dashboardCollab.history.failed') }}
                        </UBadge>
                        <UBadge v-else-if="r.status === 'publishing'" color="amber" variant="soft" size="xs">
                            {{ $t('dashboardCollab.history.publishing') }}
                        </UBadge>
                        <span class="text-sm text-gray-800 dark:text-gray-200">{{ releaseLine(r) }}</span>
                    </div>
                    <p v-if="r.status === 'failed' && r.error" class="text-xs text-red-600 dark:text-red-400 break-words"
                        data-testid="history-release-error">{{ r.error }}</p>
                </div>
                <UButton v-if="canRollback && r.status === 'ready' && !r.live && confirming !== r.id"
                    size="xs" color="gray" variant="soft" icon="i-heroicons-arrow-uturn-left" class="shrink-0"
                    :disabled="!!busy" data-testid="history-rollback" @click="confirming = r.id">
                    {{ $t('dashboardCollab.history.rollback') }}
                </UButton>
            </div>

            <!-- Confirm in place: it changes what every reader sees now. -->
            <div v-if="confirming === r.id" class="mt-2 p-3 rounded-lg border border-amber-200 dark:border-amber-900 bg-amber-50 dark:bg-amber-950/40 space-y-2"
                data-testid="history-rollback-confirm">
                <p class="text-sm font-medium text-gray-900 dark:text-gray-100">
                    {{ r.version_number != null ? $t('dashboardCollab.history.rollbackConfirmTitle', { n: r.version_number }) : $t('dashboardCollab.history.rollbackConfirmTitlePlain') }}
                </p>
                <p class="text-xs text-gray-600 dark:text-gray-400">{{ $t('dashboardCollab.history.rollbackConfirmBody') }}</p>
                <div class="flex flex-wrap justify-end gap-2">
                    <UButton size="xs" color="gray" variant="ghost" :disabled="!!busy" @click="confirming = null">
                        {{ $t('common.cancel') }}
                    </UButton>
                    <UButton size="xs" color="amber" icon="i-heroicons-arrow-uturn-left" :loading="busy === r.id"
                        data-testid="history-rollback-go" @click="rollback(r)">
                        {{ $t('dashboardCollab.history.rollback') }}
                    </UButton>
                </div>
            </div>
        </li>
    </ul>
</template>

<script setup lang="ts">
import HistoryEmpty from './HistoryEmpty.vue'

const props = defineProps<{ dashboardId: string; releases: any[]; canRollback: boolean }>()
const emit = defineEmits<{ (e: 'rolled-back'): void }>()

const { t } = useI18n()
const toast = useToast()
const { getErrorMessage } = useErrorMessage()
const { relativeTime } = useRelativeTime()

const confirming = ref<string | null>(null)
const busy = ref<string | null>(null)

const time = (r: any) => new Date(r.published_at || 0).getTime()
// The live one first, then newest first (the API already orders by
// creation; a rollback makes an older one live).
const rows = computed(() => [...(props.releases || [])].sort((a, b) => Number(b.live) - Number(a.live) || time(b) - time(a)))

function releaseLine(r: any): string {
    const name = r.published_by?.name || t('dashboardCollab.history.someone')
    const when = r.published_at ? relativeTime(r.published_at) : ''
    if (r.status !== 'ready') {
        return r.version_number != null ? t('dashboardCollab.history.versionLabel', { n: r.version_number }) : ''
    }
    if (r.version_number != null) {
        return when
            ? t('dashboardCollab.history.releaseLine', { n: r.version_number, name, when })
            : t('dashboardCollab.history.releaseLineNoTime', { n: r.version_number, name })
    }
    return when
        ? t('dashboardCollab.history.releaseLineNoVersion', { name, when })
        : t('dashboardCollab.history.releaseLineNoVersionNoTime', { name })
}

async function rollback(r: any) {
    busy.value = r.id
    try {
        const res = await useMyFetch(`/dashboards/${props.dashboardId}/rollback`, { method: 'POST', body: { release_id: r.id } })
        if (res.error.value) throw res.error.value
        toast.add({
            title: r.version_number != null
                ? t('dashboardCollab.history.rollbackDone', { n: r.version_number })
                : t('dashboardCollab.history.rollbackDonePlain'),
            color: 'green',
        })
        confirming.value = null
        emit('rolled-back')
    } catch (e) {
        toast.add({ title: t('dashboardCollab.history.rollbackFailed'), description: getErrorMessage(e), color: 'red' })
    } finally {
        busy.value = null
    }
}
</script>
