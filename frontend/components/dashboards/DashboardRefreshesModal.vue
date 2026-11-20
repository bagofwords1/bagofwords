<template>
    <!-- "Refresh failing" for an editor with no conversation of their own on
         the dashboard: its refreshes (refhist01) right on the list, the newest
         failure open. Nothing is copied or run -- the editor is one more
         click, and only when asked for. -->
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-3xl' }">
        <div class="p-5" data-testid="dashboard-refreshes-modal">
            <div class="flex items-start justify-between gap-3 mb-4">
                <div class="min-w-0">
                    <h2 class="text-base font-medium text-ink truncate">
                        <bdi>{{ title }}</bdi>
                        <span class="text-ink-muted font-normal"> · {{ $t('dashboardCollab.refreshes.tab') }}</span>
                    </h2>
                    <p class="mt-0.5 text-xs text-ink-muted">{{ $t('dashboardCollab.refreshes.desc') }}</p>
                </div>
                <UButton color="gray" variant="ghost" size="xs" square icon="i-heroicons-x-mark"
                    :aria-label="$t('common.close')" @click="isOpen = false" />
            </div>

            <HistoryRefreshes v-if="dashboard" :dashboard-id="dashboard.id" :active="isOpen" expand-failure />

            <div v-if="editable" class="mt-4 flex justify-end">
                <UButton color="gray" variant="soft" size="sm" icon="i-heroicons-pencil-square"
                    :loading="editing" :disabled="editing" data-testid="dashboard-refreshes-open-editor"
                    @click="emit('edit')">
                    {{ $t('dashboards.refreshes.openEditor') }}
                </UButton>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
import HistoryRefreshes from '~/components/dashboard/history/HistoryRefreshes.vue'
import type { DashboardCardData } from '~/utils/dashboardList'

const props = defineProps<{ dashboard: DashboardCardData | null; editing?: boolean }>()
const isOpen = defineModel<boolean>({ default: false })
const emit = defineEmits<{ (e: 'edit'): void }>()

const { t } = useI18n()
const title = computed(() => props.dashboard?.title || t('dashboardCollab.page.untitled'))
const editable = computed(() => props.dashboard?.role === 'owner' || props.dashboard?.role === 'edit')
</script>
