<template>
    <!-- The conversations that edited the dashboard, newest first: whose,
         from when to when, how many versions, on which draft. Only someone
         who can open a conversation gets its link. -->
    <HistoryEmpty v-if="!rows.length" icon="heroicons:chat-bubble-left-right" :text="tm('dashboardCollab.history.noConversations', mode)" />
    <ul v-else class="divide-y divide-gray-100 dark:divide-gray-800" data-testid="history-conversations">
        <li v-for="c in rows" :key="c.report_id" class="flex items-center justify-between gap-3 py-2.5" data-testid="history-conversation">
            <div class="min-w-0 flex-1 space-y-0.5">
                <div class="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-sm">
                    <span class="font-medium text-gray-900 dark:text-gray-100 truncate">{{ c.user?.name || $t('dashboardCollab.history.someone') }}</span>
                    <template v-if="c.draft">
                        <span class="text-gray-300 dark:text-gray-600" aria-hidden="true">·</span>
                        <span class="text-gray-500 dark:text-gray-400 truncate">{{ c.draft }}</span>
                    </template>
                    <UBadge v-if="!c.ended_at" color="primary" variant="soft" size="xs">{{ $t('dashboardCollab.history.stillEditing') }}</UBadge>
                </div>
                <div class="flex flex-wrap items-center gap-x-1.5 text-xs text-gray-500 dark:text-gray-400">
                    <span v-if="c.started_at && c.ended_at">{{ $t('dashboardCollab.history.conversationRange', { from: relativeTime(c.started_at), to: relativeTime(c.ended_at) }) }}</span>
                    <span v-else-if="c.started_at">{{ $t('dashboardCollab.history.conversationSince', { from: relativeTime(c.started_at) }) }}</span>
                    <span v-if="c.started_at" class="text-gray-300 dark:text-gray-600" aria-hidden="true">·</span>
                    <span>{{ $t('dashboardCollab.history.versionsCount', c.versions || 0) }}</span>
                </div>
            </div>
            <UButton v-if="c.can_open" :to="`/reports/${c.report_id}`" size="xs" color="gray" variant="soft"
                icon="i-heroicons-arrow-top-right-on-square" class="shrink-0" data-testid="history-conversation-open">
                {{ $t('dashboardCollab.history.openConversation') }}
            </UButton>
        </li>
    </ul>
</template>

<script setup lang="ts">
import HistoryEmpty from './HistoryEmpty.vue'

const props = defineProps<{ conversations: any[]; mode?: string | null }>()
const { tm } = useArtifactWording()
const { relativeTime } = useRelativeTime()

const time = (v: any) => new Date(v || 0).getTime()
const rows = computed(() => [...(props.conversations || [])].sort((a, b) => time(b.started_at) - time(a.started_at)))
</script>
