<template>
  <div class="px-6 py-4">
    <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">{{ $t('skillCatalog.subtitle') }}</p>

    <div
      v-if="!canManage"
      class="mb-3 flex items-start gap-2 px-3 py-2 rounded-lg bg-gray-50 dark:bg-gray-800/50 text-[11px] text-gray-500 dark:text-gray-400"
    >
      <UIcon name="i-heroicons-lock-closed" class="w-3.5 h-3.5 shrink-0 mt-px" />
      <span>{{ $t('skillCatalog.adminOnly') }}</span>
    </div>

    <div class="relative mb-3">
      <UIcon
        name="i-heroicons-magnifying-glass"
        class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500 absolute top-1/2 -translate-y-1/2 start-2 pointer-events-none"
      />
      <input
        v-model="search"
        type="text"
        data-testid="skill-catalog-search"
        :placeholder="$t('skillCatalog.searchPlaceholder')"
        class="w-full h-8 rounded-md border border-gray-200 dark:border-gray-800 bg-transparent text-xs text-gray-800 dark:text-gray-200 placeholder-gray-400 dark:placeholder-gray-600 focus:outline-none focus:ring-1 focus:ring-gray-300 dark:focus:ring-gray-700"
        style="padding-inline-start: 26px; padding-inline-end: 8px"
      />
    </div>

    <div v-if="loading" class="flex items-center gap-2 py-8 text-xs text-gray-400 dark:text-gray-500">
      <Spinner class="w-3.5 h-3.5" /><span>{{ $t('skillCatalog.loading') }}</span>
    </div>
    <div v-else-if="error" class="py-8 text-xs text-red-500 dark:text-red-400">{{ error }}</div>
    <div v-else-if="filtered.length === 0" class="py-8 text-xs text-gray-400 dark:text-gray-500">
      {{ $t('skillCatalog.noResults') }}
    </div>

    <div v-else class="border border-gray-100 dark:border-gray-800 rounded-lg overflow-hidden">
      <div
        v-for="(skill, i) in filtered"
        :key="skill.key"
        :data-testid="`skill-row-${skill.key}`"
        class="transition-colors"
        :class="i > 0 ? 'border-t border-gray-100 dark:border-gray-800' : ''"
      >
        <div
          class="flex items-start gap-3 px-3 py-2.5 cursor-pointer"
          :class="expanded === skill.key ? 'bg-gray-50 dark:bg-gray-800/50' : 'hover:bg-gray-50/70 dark:hover:bg-gray-800/30'"
          @click="toggleExpand(skill.key)"
        >
          <UIcon
            name="i-heroicons-chevron-right"
            class="w-3 h-3 mt-1 shrink-0 text-gray-300 dark:text-gray-600 transition-transform"
            :class="expanded === skill.key ? 'rotate-90' : 'rtl:rotate-180'"
          />
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-1.5 flex-wrap">
              <span class="text-[13px] text-gray-800 dark:text-gray-200">{{ skill.title }}</span>
              <span
                v-if="skill.installed"
                data-testid="skill-enabled-badge"
                class="text-[10px] text-gray-400 dark:text-gray-500"
              >{{ $t('skillCatalog.enabled') }}</span>
              <span
                v-if="skill.update_available"
                class="inline-flex items-center px-1.5 h-4 rounded bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-300 text-[10px] font-medium"
              >{{ $t('skillCatalog.updateAvailable') }}</span>
              <span
                v-if="skill.is_customized"
                class="text-[10px] text-gray-400 dark:text-gray-500"
                :title="$t('skillCatalog.customizedHint')"
              >{{ $t('skillCatalog.customized') }}</span>
            </div>
            <p class="mt-0.5 text-[11px] text-gray-400 dark:text-gray-500">{{ skill.description }}</p>
            <div v-if="expanded === skill.key" class="flex items-center gap-1.5 mt-1.5 flex-wrap">
              <span class="text-[10px] text-gray-400 dark:text-gray-500">v{{ skill.version }}</span>
              <span v-for="tag in skill.tags" :key="tag" :class="chip">{{ tag }}</span>
              <span
                v-if="skill.modes?.length"
                :class="chip"
                :title="$t('skillCatalog.modeScopedHint')"
              >{{ $t('skillCatalog.modeScoped', { modes: skill.modes.join(', ') }) }}</span>
            </div>
          </div>

          <div class="shrink-0 flex items-center gap-1.5" @click.stop>
            <button
              v-if="skill.installed && skill.update_available && canManage"
              type="button"
              :disabled="busyKey === skill.key"
              class="h-7 px-2.5 rounded-md text-[11px] font-medium text-amber-700 dark:text-amber-400 hover:bg-amber-50 dark:hover:bg-amber-500/10 disabled:opacity-50"
              @click="update(skill)"
            >{{ $t('skillCatalog.update') }}</button>
            <button
              type="button"
              :data-testid="`skill-toggle-${skill.key}`"
              :disabled="!canManage || busyKey === skill.key"
              class="h-7 px-3 rounded-md text-[11px] font-medium border disabled:opacity-50 disabled:cursor-not-allowed"
              :class="skill.installed
                ? 'border-gray-200 dark:border-gray-800 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50'
                : 'border-gray-900 dark:border-gray-100 bg-gray-900 dark:bg-gray-100 text-white dark:text-gray-900 hover:bg-gray-800 dark:hover:bg-white'"
              :title="canManage ? '' : $t('skillCatalog.adminOnly')"
              @click="toggle(skill)"
            >
              <Spinner v-if="busyKey === skill.key" class="w-3 h-3" />
              <template v-else>{{ skill.installed ? $t('skillCatalog.disable') : $t('skillCatalog.enable') }}</template>
            </button>
          </div>
        </div>

        <!-- The shipped playbook, so it can be read before anyone enables it. -->
        <div
          v-if="expanded === skill.key"
          class="px-3 pb-4 ps-9 border-t border-gray-100 dark:border-gray-800 bg-gray-50/40 dark:bg-gray-800/20"
        >
          <div class="pt-3 prose-instruction">
            <InstructionsInstructionText :text="skill.body" :prose="true" :markdown="true" />
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'

interface CatalogEntry {
  key: string
  title: string
  description: string
  category: string
  version: string
  body: string
  tags: string[]
  modes: string[]
  installed: boolean
  instruction_id: string | null
  installed_version: string | null
  update_available: boolean
  is_customized: boolean
}

const emit = defineEmits<{
  // `removedId` is the instruction row a disable just deleted, so the tree can
  // drop it — a re-fetch alone cannot, because rows are merged by id and a
  // deleted row is simply absent from the response.
  (e: 'changed', payload: { removedId?: string | null }): void
}>()

const { t } = useI18n()
const toast = useToast()

// Enabling a skill creates a global, org-wide row, so it takes org-level
// manage_instructions — the same authority as a global instruction. Everyone
// else browses read-only.
const canManage = computed(() => useCan('manage_instructions'))

const chip = 'inline-flex items-center px-1.5 h-4 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 text-[10px]'

const skills = ref<CatalogEntry[]>([])
const loading = ref(false)
const error = ref('')
const busyKey = ref<string | null>(null)
const search = ref('')
const expanded = ref<string | null>(null)

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return skills.value
  return skills.value.filter(s =>
    s.title.toLowerCase().includes(q)
    || s.description.toLowerCase().includes(q)
    || (s.tags || []).some(tag => tag.toLowerCase().includes(q)),
  )
})

const toggleExpand = (key: string) => { expanded.value = expanded.value === key ? null : key }

const load = async () => {
  loading.value = true
  error.value = ''
  try {
    const { data, error: err } = await useMyFetch<CatalogEntry[]>('/instructions/skill-catalog')
    if (err.value) throw err.value
    skills.value = (data.value as CatalogEntry[]) || []
  } catch (e: any) {
    error.value = t('skillCatalog.loadFailed')
  } finally {
    loading.value = false
  }
}

// Replace one entry in place so the list does not jump while the user works
// through it.
const applyResult = (result: CatalogEntry) => {
  const i = skills.value.findIndex(s => s.key === result.key)
  if (i !== -1) skills.value[i] = { ...skills.value[i], ...result }
}

const toggle = async (skill: CatalogEntry) => {
  if (!canManage.value || busyKey.value) return
  busyKey.value = skill.key
  const enabling = !skill.installed
  const removedId = enabling ? null : skill.instruction_id
  try {
    const { data, error: err } = await useMyFetch<CatalogEntry>(
      enabling
        ? `/instructions/skill-catalog/${skill.key}/install`
        : `/instructions/skill-catalog/${skill.key}`,
      { method: enabling ? 'POST' : 'DELETE' },
    )
    if (err.value) throw err.value
    if (data.value) applyResult(data.value as CatalogEntry)
    toast.add({
      title: enabling
        ? t('skillCatalog.enabledToast', { name: skill.title })
        : t('skillCatalog.disabledToast', { name: skill.title }),
    })
    emit('changed', { removedId })
  } catch (e: any) {
    toast.add({ title: t('skillCatalog.actionFailed'), color: 'red' })
  } finally {
    busyKey.value = null
  }
}

const update = async (skill: CatalogEntry) => {
  if (!canManage.value || busyKey.value) return
  // Updating overwrites the org's copy, so a customized skill needs consent.
  if (skill.is_customized && !confirm(t('skillCatalog.confirmOverwrite', { name: skill.title }))) return
  busyKey.value = skill.key
  try {
    const { data, error: err } = await useMyFetch<CatalogEntry>(
      `/instructions/skill-catalog/${skill.key}/update`, { method: 'POST' },
    )
    if (err.value) throw err.value
    if (data.value) applyResult(data.value as CatalogEntry)
    toast.add({ title: t('skillCatalog.updatedToast', { name: skill.title }) })
    emit('changed', {})
  } catch (e: any) {
    toast.add({ title: t('skillCatalog.actionFailed'), color: 'red' })
  } finally {
    busyKey.value = null
  }
}

onMounted(load)
</script>
