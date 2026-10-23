<template>
    <!-- Every completed version, newest first: who made it, when, on which
         draft, whether it was published; each opens on the preview page. -->
    <HistoryEmpty v-if="!rows.length" icon="heroicons:clock" :text="$t('dashboardCollab.history.noVersions')" />
    <ul v-else class="divide-y divide-gray-100 dark:divide-gray-800" data-testid="history-versions">
        <li v-for="v in rows" :key="v.id" class="flex items-center justify-between gap-3 py-2.5" data-testid="history-version">
            <div class="min-w-0 flex-1">
                <div class="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-sm">
                    <span class="font-medium text-gray-900 dark:text-gray-100 tabular-nums">{{ $t('dashboardCollab.history.versionLabel', { n: v.number }) }}</span>
                    <span class="text-gray-300 dark:text-gray-600" aria-hidden="true">·</span>
                    <span class="text-gray-700 dark:text-gray-300 truncate">{{ v.author?.name || $t('dashboardCollab.history.someone') }}</span>
                    <template v-if="v.created_at">
                        <span class="text-gray-300 dark:text-gray-600" aria-hidden="true">·</span>
                        <span class="text-gray-500 dark:text-gray-400 whitespace-nowrap">{{ relativeTime(v.created_at) }}</span>
                    </template>
                    <template v-if="v.draft">
                        <span class="text-gray-300 dark:text-gray-600" aria-hidden="true">·</span>
                        <span class="text-gray-500 dark:text-gray-400 truncate">{{ v.draft }}</span>
                    </template>
                    <UBadge v-if="v.published" color="green" variant="soft" size="xs" data-testid="history-version-published">
                        {{ $t('dashboardCollab.history.publishedBadge') }}
                    </UBadge>
                </div>
            </div>
            <UButton size="xs" color="gray" variant="soft" icon="i-heroicons-eye" class="shrink-0"
                data-testid="history-version-preview" @click="emit('preview', v.number)">
                {{ $t('dashboardCollab.history.preview') }}
            </UButton>
        </li>
    </ul>
</template>

<script setup lang="ts">
import HistoryEmpty from './HistoryEmpty.vue'

const props = defineProps<{ versions: any[] }>()
const emit = defineEmits<{ (e: 'preview', number: number): void }>()
const { relativeTime } = useRelativeTime()

const rows = computed(() => [...(props.versions || [])].sort((a, b) => (b.number ?? 0) - (a.number ?? 0)))
</script>
