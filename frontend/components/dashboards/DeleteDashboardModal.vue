<template>
    <!-- The owner deletes a dashboard (DELETE /artifacts/{id}/dashboard): it and
         every version of it. What else goes with it is said before they confirm:
         a published one's link stops working, sharing ends, a schedule stops.
         The conversations it was made in stay. -->
    <UModal v-model="isOpen" :prevent-close="deleting" :ui="{ width: 'sm:max-w-md' }">
        <div class="p-5" data-testid="delete-dashboard-modal">
            <div class="flex items-start gap-3">
                <div class="shrink-0 w-9 h-9 rounded-full bg-red-100 dark:bg-red-900/40 flex items-center justify-center">
                    <UIcon name="i-heroicons-trash" class="w-5 h-5 text-red-600 dark:text-red-400" aria-hidden="true" />
                </div>
                <div class="min-w-0">
                    <h2 class="text-base font-medium text-ink">{{ tm('dashboardDelete.title', dashboard?.mode) }}</h2>
                    <p class="mt-1 text-sm text-ink-2 break-words">
                        {{ $t('dashboardDelete.body', { title: isolate(title) }) }}
                    </p>
                </div>
            </div>

            <ul v-if="warnings.length" class="mt-4 space-y-1.5 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 p-3" data-testid="delete-dashboard-warnings">
                <li v-for="key in warnings" :key="key" class="flex items-start gap-2 text-sm text-amber-800 dark:text-amber-200">
                    <UIcon name="i-heroicons-exclamation-triangle" class="w-4 h-4 mt-0.5 shrink-0" aria-hidden="true" />
                    <span>{{ $t(`dashboardDelete.${key}`) }}</span>
                </li>
            </ul>

            <div class="mt-5 flex justify-end gap-2">
                <UButton color="gray" variant="ghost" :disabled="deleting" data-testid="delete-dashboard-cancel" @click="isOpen = false">
                    {{ $t('common.cancel') }}
                </UButton>
                <UButton color="red" :loading="deleting" data-testid="delete-dashboard-confirm" @click="remove">
                    {{ $t('dashboardDelete.confirm') }}
                </UButton>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
import { isolate } from '~/utils/dashboardList'

// What the confirmation needs to say: the settings window passes what it loaded.
export interface DeletableDashboard {
    id: string
    title?: string | null
    mode?: string | null
    published?: boolean
    visibility?: string | null
    cron_schedule?: string | null
}

const props = defineProps<{ dashboard: DeletableDashboard | null }>()
const isOpen = defineModel<boolean>({ default: false })
const emit = defineEmits<{ (e: 'deleted', id: string): void }>()

const { t } = useI18n()
const { tm } = useArtifactWording()
const toast = useToast()
const { getErrorMessage } = useErrorMessage()

const deleting = ref(false)
const title = computed(() => props.dashboard?.title || t('dashboardCollab.page.untitled'))
const warnings = computed(() => {
    const d = props.dashboard
    if (!d) return []
    const out: string[] = []
    if (d.published) out.push('publishedWarning')
    if (d.visibility && d.visibility !== 'none') out.push('sharedWarning')
    if (d.cron_schedule) out.push('scheduleWarning')
    return out
})

async function remove() {
    const d = props.dashboard
    if (!d || deleting.value) return
    deleting.value = true
    try {
        const { error } = await useMyFetch(`/artifacts/${encodeURIComponent(d.id)}/dashboard`, { method: 'DELETE' })
        if (error.value) throw error.value
        toast.add({ title: t('dashboardDelete.deleted', { title: isolate(title.value) }), color: 'green' })
        isOpen.value = false
        emit('deleted', d.id)
    } catch (error) {
        toast.add({ title: t('dashboardDelete.failed'), description: getErrorMessage(error), color: 'red' })
    } finally {
        deleting.value = false
    }
}
</script>
