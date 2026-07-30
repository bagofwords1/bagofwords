<template>
    <div class="max-w-3xl mx-auto px-4 py-8">
        <!-- Header -->
        <div class="flex items-center justify-between mb-6">
            <div>
                <h1 class="text-xl font-semibold text-gray-900 dark:text-white">{{ $t('projects.title') }}</h1>
            </div>
            <button
                type="button"
                name="new-project"
                class="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium px-3 py-1.5 transition-colors"
                @click="createOpen = true"
            >
                <UIcon name="i-heroicons-plus" class="w-4 h-4" />
                {{ $t('projects.newProject') }}
            </button>
        </div>

        <!-- Directory list: every project the user owns or was granted -->
        <div v-if="projects.length" class="divide-y divide-gray-100 dark:divide-gray-800 rounded-xl border border-gray-200 dark:border-gray-800 overflow-hidden">
            <NuxtLink
                v-for="project in projects"
                :key="project.id"
                :to="`/projects/${project.id}`"
                class="flex items-center gap-3 px-4 py-3 bg-white dark:bg-gray-900 hover:bg-gray-50 dark:hover:bg-gray-800/60 transition-colors group"
                :data-testid="`project-row-${project.id}`"
            >
                <span class="flex items-center justify-center w-9 h-9 rounded-lg bg-gray-50 dark:bg-gray-800 shrink-0">
                    <UIcon
                        name="i-heroicons-folder"
                        class="w-5 h-5"
                        :class="project.color ? '' : 'text-gray-400 dark:text-gray-500'"
                        :style="project.color ? { color: project.color } : undefined"
                    />
                </span>
                <span class="flex-1 min-w-0">
                    <span class="block text-sm font-medium text-gray-900 dark:text-white truncate">{{ project.name }}</span>
                    <span v-if="project.description" class="block text-xs text-gray-400 dark:text-gray-500 truncate">{{ project.description }}</span>
                </span>
                <span class="flex items-center gap-3 shrink-0 text-xs text-gray-400 dark:text-gray-500">
                    <span>{{ $t('projects.reportCount', { count: project.report_count }, project.report_count) }}</span>
                    <span v-if="!project.is_owner && (project as any).user?.name" class="hidden sm:inline">
                        {{ $t('projects.ownedBy', { name: (project as any).user.name }) }}
                    </span>
                    <span
                        v-if="!project.is_owner || project.member_count > 0 || project.access === 'org'"
                        class="inline-flex items-center gap-1 rounded-full border border-gray-200 dark:border-gray-700 px-2 py-0.5 text-[11px]"
                    >
                        <UIcon name="i-heroicons-user-group" class="w-3 h-3" />
                        {{ $t('projects.sharedBadge') }}
                    </span>
                    <UIcon name="i-heroicons-chevron-right" class="w-4 h-4 rtl-flip opacity-0 group-hover:opacity-100 transition-opacity" />
                </span>
            </NuxtLink>
        </div>

        <!-- Empty state -->
        <div v-else-if="loaded" class="text-center py-16 border border-dashed border-gray-200 dark:border-gray-800 rounded-xl">
            <UIcon name="i-heroicons-folder-plus" class="w-8 h-8 text-gray-300 dark:text-gray-600 mx-auto mb-2" />
            <p class="text-sm text-gray-400 dark:text-gray-500 mb-4">{{ $t('projects.moveNoProjects') }}</p>
            <button
                type="button"
                class="inline-flex items-center gap-1.5 rounded-lg border border-gray-200 dark:border-gray-700 text-sm text-gray-700 dark:text-gray-200 px-3 py-1.5 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
                @click="createOpen = true"
            >
                <UIcon name="i-heroicons-plus" class="w-4 h-4" />
                {{ $t('projects.newProject') }}
            </button>
        </div>

        <!-- Create dialog (name only — everything else lives in settings) -->
        <UModal v-model="createOpen" :ui="{ width: 'sm:max-w-sm' }">
            <div class="p-5">
                <h3 class="text-sm font-semibold text-gray-900 dark:text-white mb-1">{{ $t('projects.createTitle') }}</h3>
                <p class="text-xs text-gray-400 dark:text-gray-500 mb-3">{{ $t('projects.createHint') }}</p>
                <input
                    v-model="createName"
                    type="text"
                    :placeholder="$t('projects.namePlaceholder')"
                    class="w-full rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                    @keyup.enter="submitCreate"
                />
                <div class="flex justify-end gap-2 mt-4">
                    <button type="button" class="text-sm text-gray-500 dark:text-gray-400 px-3 py-1.5 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800" @click="createOpen = false">
                        {{ $t('common.cancel') }}
                    </button>
                    <button
                        type="button"
                        class="text-sm font-medium text-white bg-blue-600 hover:bg-blue-700 px-3 py-1.5 rounded-lg disabled:opacity-50"
                        :disabled="!createName.trim() || createBusy"
                        @click="submitCreate"
                    >
                        {{ $t('projects.create') }}
                    </button>
                </div>
            </div>
        </UModal>
    </div>
</template>

<script setup lang="ts">
import { useProjects } from '~/composables/useProjects'

definePageMeta({ auth: true })

const { t } = useI18n()
useHead({ title: () => t('projects.title') })

const router = useRouter()
const toast = useToast()
const { projects, loaded, fetchProjects, createProject } = useProjects()

const createOpen = ref(false)
const createName = ref('')
const createBusy = ref(false)

const submitCreate = async () => {
    const name = createName.value.trim()
    if (!name || createBusy.value) return
    createBusy.value = true
    try {
        const project: any = await createProject({ name })
        createOpen.value = false
        createName.value = ''
        if (project?.id) await router.push(`/projects/${project.id}`)
    } catch (e: any) {
        toast.add({ title: t('common.error'), description: String(e?.data?.detail || e?.message || ''), color: 'red' })
    } finally {
        createBusy.value = false
    }
}

onMounted(() => { fetchProjects() })
</script>
