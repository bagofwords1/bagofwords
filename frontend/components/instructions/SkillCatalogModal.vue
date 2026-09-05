<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-3xl' }">
    <div class="p-5">
      <!-- Header -->
      <div class="flex items-start justify-between gap-3 mb-3">
        <div class="min-w-0">
          <div class="text-sm font-semibold text-gray-900 dark:text-white flex items-center gap-1.5">
            <UIcon name="i-heroicons-squares-plus" class="w-4 h-4 text-gray-400 dark:text-gray-500" />
            {{ $t('skillCatalog.title') }}
          </div>
          <div class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('skillCatalog.subtitle') }}</div>
        </div>
        <button
          type="button"
          class="shrink-0 w-6 h-6 rounded flex items-center justify-center text-gray-400 dark:text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800"
          data-testid="skill-catalog-close"
          :aria-label="$t('skillCatalog.close')"
          @click="isOpen = false"
        >
          <UIcon name="i-heroicons-x-mark" class="w-4 h-4" />
        </button>
      </div>

      <!-- Read-only notice for non-admins -->
      <div
        v-if="!canManage"
        class="mb-3 flex items-start gap-2 px-3 py-2 rounded-lg bg-gray-50 dark:bg-gray-800/50 text-[11px] text-gray-500 dark:text-gray-400"
      >
        <UIcon name="i-heroicons-lock-closed" class="w-3.5 h-3.5 shrink-0 mt-px" />
        <span>{{ $t('skillCatalog.adminOnly') }}</span>
      </div>

      <!-- Search -->
      <div class="relative mb-3">
        <UIcon
          name="i-heroicons-magnifying-glass"
          class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500 absolute top-1/2 -translate-y-1/2 start-2.5 pointer-events-none"
        />
        <input
          v-model="search"
          type="text"
          data-testid="skill-catalog-search"
          :placeholder="$t('skillCatalog.searchPlaceholder')"
          class="w-full h-8 rounded-lg border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 text-xs text-gray-800 dark:text-gray-200 placeholder-gray-400 dark:placeholder-gray-600 focus:outline-none focus:ring-1 focus:ring-blue-400"
          style="padding-inline-start: 28px; padding-inline-end: 10px"
        />
      </div>

      <!-- List -->
      <div v-if="loading" class="flex items-center gap-2 py-8 justify-center text-xs text-gray-400 dark:text-gray-500">
        <Spinner class="w-4 h-4" />
        <span>{{ $t('skillCatalog.loading') }}</span>
      </div>
      <div v-else-if="error" class="py-8 text-center text-xs text-red-500 dark:text-red-400">{{ error }}</div>
      <div v-else-if="filtered.length === 0" class="py-8 text-center text-xs text-gray-400 dark:text-gray-500">
        {{ $t('skillCatalog.noResults') }}
      </div>
      <div v-else class="max-h-[55vh] overflow-y-auto -mx-1 px-1 space-y-2">
        <div
          v-for="skill in filtered"
          :key="skill.key"
          :data-testid="`skill-card-${skill.key}`"
          class="border rounded-lg p-3 transition-colors"
          :class="skill.installed
            ? 'border-blue-200 dark:border-blue-500/30 bg-blue-50/40 dark:bg-blue-500/5'
            : 'border-gray-200 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800/40'"
        >
          <div class="flex items-start gap-3">
            <span
              class="shrink-0 inline-flex items-center justify-center w-7 h-7 rounded-md border"
              :class="skill.installed
                ? 'border-blue-200 dark:border-blue-500/30 text-blue-500 dark:text-blue-400'
                : 'border-gray-200 dark:border-gray-800 text-gray-400 dark:text-gray-500'"
            >
              <UIcon name="i-heroicons-sparkles" class="w-3.5 h-3.5" />
            </span>

            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-1.5 flex-wrap">
                <span class="text-[13px] font-medium text-gray-900 dark:text-white">{{ skill.title }}</span>
                <span
                  v-if="skill.installed"
                  data-testid="skill-enabled-badge"
                  class="inline-flex items-center px-1.5 h-4 rounded bg-blue-100 dark:bg-blue-500/20 text-blue-700 dark:text-blue-300 text-[10px] font-medium"
                >{{ $t('skillCatalog.enabled') }}</span>
                <span
                  v-if="skill.update_available"
                  class="inline-flex items-center px-1.5 h-4 rounded bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-300 text-[10px] font-medium"
                >{{ $t('skillCatalog.updateAvailable') }}</span>
                <span
                  v-if="skill.is_customized"
                  class="inline-flex items-center px-1.5 h-4 rounded bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-300 text-[10px] font-medium"
                  :title="$t('skillCatalog.customizedHint')"
                >{{ $t('skillCatalog.customized') }}</span>
              </div>

              <p class="text-[11px] text-gray-500 dark:text-gray-400 mt-1 leading-relaxed">{{ skill.description }}</p>

              <div class="flex items-center gap-1.5 mt-1.5 flex-wrap">
                <span class="text-[10px] text-gray-400 dark:text-gray-500">v{{ skill.version }}</span>
                <span
                  v-for="tag in skill.tags"
                  :key="tag"
                  class="inline-flex items-center px-1.5 h-4 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 text-[10px]"
                >{{ tag }}</span>
                <span
                  v-if="skill.modes && skill.modes.length"
                  class="inline-flex items-center px-1.5 h-4 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 text-[10px]"
                  :title="$t('skillCatalog.modeScopedHint')"
                >{{ $t('skillCatalog.modeScoped', { modes: skill.modes.join(', ') }) }}</span>
              </div>
            </div>

            <!-- Actions -->
            <div class="shrink-0 flex items-center gap-1.5">
              <button
                v-if="skill.installed && skill.update_available && canManage"
                type="button"
                :disabled="busyKey === skill.key"
                class="h-7 px-2.5 rounded-md border border-amber-200 dark:border-amber-500/30 text-amber-700 dark:text-amber-300 text-[11px] font-medium hover:bg-amber-50 dark:hover:bg-amber-500/10 disabled:opacity-50"
                @click="update(skill)"
              >{{ $t('skillCatalog.update') }}</button>
              <button
                type="button"
                :data-testid="`skill-toggle-${skill.key}`"
                :disabled="!canManage || busyKey === skill.key"
                class="h-7 px-3 rounded-md text-[11px] font-medium border disabled:opacity-50 disabled:cursor-not-allowed"
                :class="skill.installed
                  ? 'border-gray-200 dark:border-gray-800 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50'
                  : 'border-blue-500 bg-blue-500 text-white hover:bg-blue-600'"
                :title="canManage ? '' : $t('skillCatalog.adminOnly')"
                @click="toggle(skill)"
              >
                <Spinner v-if="busyKey === skill.key" class="w-3 h-3" />
                <template v-else>{{ skill.installed ? $t('skillCatalog.disable') : $t('skillCatalog.enable') }}</template>
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </UModal>
</template>

<script setup lang="ts">
interface CatalogEntry {
  key: string
  title: string
  description: string
  category: string
  version: string
  tags: string[]
  modes: string[]
  installed: boolean
  instruction_id: string | null
  installed_version: string | null
  update_available: boolean
  is_customized: boolean
}

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  // `removedId` is the instruction row a disable just deleted, so the caller can
  // drop it from its list — a re-fetch alone cannot, because the tree merges
  // rows by id and a deleted row is simply absent from the response.
  (e: 'changed', payload: { removedId?: string | null }): void
}>()

const { t } = useI18n()
const toast = useToast()

const isOpen = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

// Enabling a skill creates a global, org-wide row, so it takes org-level
// manage_instructions — the same authority as a global instruction. Members
// can browse the catalog read-only.
const canManage = computed(() => useCan('manage_instructions'))

const skills = ref<CatalogEntry[]>([])
const loading = ref(false)
const error = ref('')
const busyKey = ref<string | null>(null)
const search = ref('')

const filtered = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return skills.value
  return skills.value.filter(s =>
    s.title.toLowerCase().includes(q) ||
    s.description.toLowerCase().includes(q) ||
    (s.tags || []).some(tag => tag.toLowerCase().includes(q)),
  )
})

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
// through it — a full reload reorders nothing but does reset scroll.
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

watch(isOpen, (open) => { if (open) load() })
</script>
