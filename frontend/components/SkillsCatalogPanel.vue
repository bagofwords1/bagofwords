<template>
  <div class="flex flex-col h-full">
    <!-- Header -->
    <div class="h-11 shrink-0 px-4 flex items-center justify-between border-b border-gray-100 dark:border-gray-800">
      <div class="flex items-center gap-2 min-w-0">
        <UIcon name="i-heroicons-sparkles" class="w-4 h-4 text-gray-400 dark:text-gray-500 shrink-0" />
        <span class="text-sm font-medium text-gray-900 dark:text-white truncate">{{ $t('agentsPage.skills') }}</span>
        <span v-if="!loading" class="text-xs tabular-nums text-gray-400 dark:text-gray-500">{{ items.length }}</span>
      </div>
    </div>

    <!-- Body -->
    <div class="flex-1 overflow-y-auto px-4 sm:px-6 py-5">
      <p class="text-sm text-gray-500 dark:text-gray-400 mb-5">{{ $t('agentsPage.skillsCatalogHint') }}</p>

      <div v-if="loading" class="flex items-center gap-2 text-[13px] text-gray-400 dark:text-gray-500">
        <Spinner class="w-4 h-4" /><span>{{ $t('agentsPage.loading') }}</span>
      </div>

      <div v-else-if="items.length === 0"
        class="rounded-xl border border-dashed border-gray-200 dark:border-gray-700 bg-gray-50/40 dark:bg-gray-800/40 px-6 py-8 text-center">
        <p class="text-sm text-gray-500 dark:text-gray-400">{{ $t('agentsPage.noSkills') }}</p>
      </div>

      <div v-else class="space-y-2">
        <div v-for="skill in items" :key="skill.key"
          class="group w-full flex items-center gap-3 rounded-xl bg-gray-50 dark:bg-gray-800/50 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors px-3 py-3"
          :class="skill.installed ? 'cursor-pointer' : ''"
          @click="skill.installed && skill.instruction_id ? emit('open-instruction', skill.instruction_id) : undefined">
          <!-- Icon tile -->
          <div
            class="shrink-0 w-10 h-10 rounded-lg bg-white dark:bg-gray-900 ring-1 ring-gray-200/70 dark:ring-gray-700 flex items-center justify-center">
            <UIcon :name="skill.icon || 'i-heroicons-sparkles'" class="w-5 h-5 text-gray-500 dark:text-gray-400" />
          </div>

          <!-- Name + description -->
          <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2 min-w-0">
              <span class="text-sm font-medium text-gray-900 dark:text-white truncate">{{ skill.name }}</span>
              <span
                class="shrink-0 inline-flex items-center px-1.5 h-4 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 text-[11px] font-medium">
                {{ skill.category }}
              </span>
            </div>
            <p class="text-[13px] text-gray-500 dark:text-gray-400 truncate mt-0.5">{{ skill.description }}</p>
          </div>

          <!-- Action -->
          <div class="shrink-0">
            <span v-if="skill.installed"
              class="inline-flex items-center gap-1 h-7 px-2.5 rounded-md bg-emerald-50 dark:bg-emerald-500/10 border border-emerald-200 dark:border-emerald-500/30 text-emerald-700 dark:text-emerald-400 text-[11px] font-medium">
              <UIcon name="i-heroicons-check" class="w-3 h-3" />
              {{ $t('agentsPage.skillInstalled') }}
            </span>
            <button v-else-if="canInstall" type="button" :disabled="installing === skill.key"
              class="inline-flex items-center gap-1 h-7 px-3 rounded-md border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-300 text-[11px] font-medium hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50 transition-colors"
              @click.stop="install(skill)">
              <Spinner v-if="installing === skill.key" class="w-3 h-3" />
              {{ installing === skill.key ? $t('agentsPage.skillInstalling') : $t('agentsPage.skillInstall') }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'

interface CatalogSkill {
  key: string
  name: string
  description: string
  category: string
  version: string
  icon: string
  installed: boolean
  instruction_id: string | null
  installed_version: string | null
}

const props = defineProps<{ canInstall: boolean }>()
const emit = defineEmits<{
  (e: 'open-instruction', id: string): void
  (e: 'installed'): void
}>()

const { t } = useI18n()
const toast = useToast()

const items = ref<CatalogSkill[]>([])
const loading = ref(true)
const installing = ref<string | null>(null)

const load = async () => {
  loading.value = true
  try {
    const { data, error } = await useMyFetch<CatalogSkill[]>('/api/skills/catalog', { method: 'GET' })
    if (error.value) throw new Error('failed')
    items.value = (data.value as CatalogSkill[]) || []
  } catch {
    toast.add({ title: t('agentsPage.toastError'), color: 'red' })
  } finally {
    loading.value = false
  }
}

const install = async (skill: CatalogSkill) => {
  installing.value = skill.key
  try {
    const { error } = await useMyFetch<any>(`/api/skills/catalog/${skill.key}/install`, { method: 'POST' })
    if (error.value) throw new Error('failed')
    await load()
    // Refresh the tree so the new skill appears under Skills with a live count.
    emit('installed')
    toast.add({ title: t('agentsPage.toastSkillInstalled') })
  } catch {
    toast.add({ title: t('agentsPage.toastError'), color: 'red' })
  } finally {
    installing.value = null
  }
}

onMounted(load)
</script>
