<template>
  <div class="py-6">
    <div class="max-w-4xl mx-auto px-4">
      <!-- Header -->
      <div class="mb-4">
        <h1 class="text-lg font-semibold text-gray-900 dark:text-white">{{ $t('skillCatalog.pageTitle') }}</h1>
        <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">{{ $t('skillCatalog.subtitle') }}</p>
      </div>

      <!-- Tabs -->
      <div class="flex items-center gap-4 border-b border-gray-200 dark:border-gray-800 mb-4">
        <button
          v-for="tab in tabs"
          :key="tab.value"
          type="button"
          :data-testid="`skills-tab-${tab.value}`"
          class="relative -mb-px pb-2 text-[13px] font-medium transition-colors border-b-2"
          :class="activeTab === tab.value
            ? 'border-blue-500 text-gray-900 dark:text-white'
            : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'"
          @click="activeTab = tab.value"
        >
          {{ tab.label }}
          <span class="ms-1 text-[11px] text-gray-400 dark:text-gray-500">{{ tab.count }}</span>
        </button>
      </div>

      <!-- Admin notice -->
      <div
        v-if="!canManage"
        class="mb-3 flex items-start gap-2 px-3 py-2 rounded-lg bg-gray-50 dark:bg-gray-800/50 text-[11px] text-gray-500 dark:text-gray-400"
      >
        <UIcon name="i-heroicons-lock-closed" class="w-3.5 h-3.5 shrink-0 mt-px" />
        <span>{{ $t('skillCatalog.adminOnly') }}</span>
      </div>

      <!-- Search -->
      <div class="relative mb-4">
        <UIcon
          name="i-heroicons-magnifying-glass"
          class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500 absolute top-1/2 -translate-y-1/2 start-2.5 pointer-events-none"
        />
        <input
          v-model="search"
          type="text"
          data-testid="skill-catalog-search"
          :placeholder="$t('skillCatalog.searchPlaceholder')"
          class="w-full h-9 rounded-lg border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 text-xs text-gray-800 dark:text-gray-200 placeholder-gray-400 dark:placeholder-gray-600 focus:outline-none focus:ring-1 focus:ring-blue-400"
          style="padding-inline-start: 30px; padding-inline-end: 10px"
        />
      </div>

      <!-- States -->
      <div v-if="loading" class="flex items-center gap-2 py-16 justify-center text-xs text-gray-400 dark:text-gray-500">
        <Spinner class="w-4 h-4" />
        <span>{{ $t('skillCatalog.loading') }}</span>
      </div>
      <div v-else-if="error" class="py-16 text-center text-xs text-red-500 dark:text-red-400">{{ error }}</div>

      <!-- Enabled tab -->
      <template v-else-if="activeTab === 'enabled'">
        <div
          v-if="filteredEnabled.length === 0"
          class="flex flex-col items-center justify-center text-center py-16 px-4"
        >
          <UIcon name="i-heroicons-sparkles" class="w-10 h-10 text-gray-300 dark:text-gray-600" />
          <h3 class="mt-3 text-sm font-medium text-gray-900 dark:text-white">
            {{ search ? $t('skillCatalog.noResults') : $t('skillCatalog.enabledEmpty') }}
          </h3>
          <p class="mt-1 max-w-sm text-xs text-gray-500 dark:text-gray-400">
            {{ search ? '' : $t('skillCatalog.enabledEmptyHint') }}
          </p>
          <button
            v-if="!search"
            type="button"
            data-testid="skills-goto-catalog"
            class="mt-5 inline-flex items-center gap-1.5 h-8 px-3 rounded-md bg-blue-600 text-white text-xs font-medium hover:bg-blue-700"
            @click="activeTab = 'catalog'"
          >
            <UIcon name="i-heroicons-squares-plus" class="w-3.5 h-3.5" />
            {{ $t('skillCatalog.browse') }}
          </button>
        </div>

        <div v-else class="space-y-2">
          <SkillsSkillCard v-for="s in filteredEnabled" :key="s.id" :skill="s" show-origin>
            <template #actions>
              <button
                v-if="s.update_available && canManage"
                type="button"
                :disabled="busyKey === s.key"
                class="h-7 px-2.5 rounded-md border border-amber-200 dark:border-amber-500/30 text-amber-700 dark:text-amber-300 text-[11px] font-medium hover:bg-amber-50 dark:hover:bg-amber-500/10 disabled:opacity-50"
                @click="update(s)"
              >{{ $t('skillCatalog.update') }}</button>
              <NuxtLink
                to="/agents"
                class="h-7 px-2.5 inline-flex items-center rounded-md border border-gray-200 dark:border-gray-800 text-gray-600 dark:text-gray-300 text-[11px] font-medium hover:bg-gray-50 dark:hover:bg-gray-800/50"
              >{{ $t('skillCatalog.edit') }}</NuxtLink>
              <button
                v-if="s.key"
                type="button"
                :data-testid="`skill-toggle-${s.key}`"
                :disabled="!canManage || busyKey === s.key"
                class="h-7 px-3 rounded-md text-[11px] font-medium border border-gray-200 dark:border-gray-800 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50 disabled:cursor-not-allowed"
                :title="canManage ? '' : $t('skillCatalog.adminOnly')"
                @click="disable(s)"
              >
                <Spinner v-if="busyKey === s.key" class="w-3 h-3" />
                <template v-else>{{ $t('skillCatalog.disable') }}</template>
              </button>
            </template>
          </SkillsSkillCard>
        </div>
      </template>

      <!-- Catalog tab -->
      <template v-else>
        <div v-if="filteredCatalog.length === 0" class="py-16 text-center text-xs text-gray-400 dark:text-gray-500">
          {{ $t('skillCatalog.noResults') }}
        </div>
        <div v-else class="space-y-2">
          <SkillsSkillCard v-for="s in filteredCatalog" :key="s.key" :skill="s" show-enabled-badge>
            <template #actions>
              <button
                v-if="s.installed && s.update_available && canManage"
                type="button"
                :disabled="busyKey === s.key"
                class="h-7 px-2.5 rounded-md border border-amber-200 dark:border-amber-500/30 text-amber-700 dark:text-amber-300 text-[11px] font-medium hover:bg-amber-50 dark:hover:bg-amber-500/10 disabled:opacity-50"
                @click="update(s)"
              >{{ $t('skillCatalog.update') }}</button>
              <button
                type="button"
                :data-testid="`skill-toggle-${s.key}`"
                :disabled="!canManage || busyKey === s.key"
                class="h-7 px-3 rounded-md text-[11px] font-medium border disabled:opacity-50 disabled:cursor-not-allowed"
                :class="s.installed
                  ? 'border-gray-200 dark:border-gray-800 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50'
                  : 'border-blue-500 bg-blue-500 text-white hover:bg-blue-600'"
                :title="canManage ? '' : $t('skillCatalog.adminOnly')"
                @click="toggle(s)"
              >
                <Spinner v-if="busyKey === s.key" class="w-3 h-3" />
                <template v-else>{{ s.installed ? $t('skillCatalog.disable') : $t('skillCatalog.enable') }}</template>
              </button>
            </template>
          </SkillsSkillCard>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'

definePageMeta({ auth: true })

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

const { t } = useI18n()
const toast = useToast()

// Enabling a skill creates a global, org-wide row, so it takes org-level
// manage_instructions — the same authority as a global instruction. Everyone
// else browses read-only.
const canManage = computed(() => useCan('manage_instructions'))

const activeTab = ref<'enabled' | 'catalog'>('enabled')
const search = ref('')
const loading = ref(false)
const error = ref('')
const busyKey = ref<string | null>(null)

const catalog = ref<CatalogEntry[]>([])
// Every kind='skill' instruction in the org — pre-built AND hand-authored, so
// the Enabled tab is the whole picture rather than only what came from here.
const installedRows = ref<any[]>([])

const enabled = computed(() => {
  const byKey = new Map(catalog.value.map(c => [c.key, c]))
  return installedRows.value.map((row) => {
    const entry = row.catalog_key ? byKey.get(row.catalog_key) : undefined
    return {
      id: row.id,
      key: row.catalog_key || null,
      title: row.title || row.preview || t('skillCatalog.untitled'),
      // A hand-authored skill may have no description; fall back to the body
      // preview, which is what the agent's catalog line would show too.
      description: row.description || row.preview || '',
      version: row.catalog_version || null,
      modes: row.applicable_modes || [],
      tags: entry?.tags || [],
      installed: true,
      update_available: entry?.update_available || false,
      is_customized: entry?.is_customized || false,
    }
  })
})

const matches = (s: Record<string, any>) => {
  const q = search.value.trim().toLowerCase()
  if (!q) return true
  return (s.title || '').toLowerCase().includes(q)
    || (s.description || '').toLowerCase().includes(q)
    || (s.tags || []).some((tag: string) => tag.toLowerCase().includes(q))
}
const filteredCatalog = computed(() => catalog.value.filter(matches))
const filteredEnabled = computed(() => enabled.value.filter(matches))

const tabs = computed(() => [
  { value: 'enabled' as const, label: t('skillCatalog.tabEnabled'), count: enabled.value.length },
  { value: 'catalog' as const, label: t('skillCatalog.tabCatalog'), count: catalog.value.length },
])

// The enabled list is server state — it includes hand-authored skills the
// catalog knows nothing about — so it is always re-read rather than patched.
const refreshInstalled = async () => {
  const { data } = await useMyFetch<any>('/instructions', {
    query: { kind: 'skill', view: 'light', limit: 2000 },
  })
  const body: any = data.value
  installedRows.value = (Array.isArray(body) ? body : body?.items) || []
}

const load = async () => {
  loading.value = true
  error.value = ''
  try {
    const cat = await useMyFetch<CatalogEntry[]>('/instructions/skill-catalog')
    if (cat.error.value) throw cat.error.value
    catalog.value = (cat.data.value as CatalogEntry[]) || []
    await refreshInstalled()
  } catch (e: any) {
    error.value = t('skillCatalog.loadFailed')
  } finally {
    loading.value = false
  }
}

const apply = (result: CatalogEntry) => {
  const i = catalog.value.findIndex(s => s.key === result.key)
  if (i !== -1) catalog.value[i] = { ...catalog.value[i], ...result }
}

const call = async (path: string, method: 'POST' | 'DELETE') => {
  const { data, error: err } = await useMyFetch<CatalogEntry>(path, { method })
  if (err.value) throw err.value
  if (data.value) apply(data.value as CatalogEntry)
}

const toggle = async (skill: CatalogEntry) => {
  if (!canManage.value || busyKey.value) return
  busyKey.value = skill.key
  const enabling = !skill.installed
  try {
    await call(
      enabling
        ? `/instructions/skill-catalog/${skill.key}/install`
        : `/instructions/skill-catalog/${skill.key}`,
      enabling ? 'POST' : 'DELETE',
    )
    toast.add({
      title: enabling
        ? t('skillCatalog.enabledToast', { name: skill.title })
        : t('skillCatalog.disabledToast', { name: skill.title }),
    })
    await refreshInstalled()
  } catch (e: any) {
    toast.add({ title: t('skillCatalog.actionFailed'), color: 'red' })
  } finally {
    busyKey.value = null
  }
}

const disable = (row: Record<string, any>) => {
  const entry = catalog.value.find(c => c.key === row.key)
  if (entry) return toggle(entry)
}

const update = async (row: Record<string, any>) => {
  if (!canManage.value || busyKey.value) return
  const entry = catalog.value.find(c => c.key === row.key)
  if (!entry) return
  // Updating overwrites the org's copy, so a customized skill needs consent.
  if (entry.is_customized && !confirm(t('skillCatalog.confirmOverwrite', { name: entry.title }))) return
  busyKey.value = entry.key
  try {
    await call(`/instructions/skill-catalog/${entry.key}/update`, 'POST')
    toast.add({ title: t('skillCatalog.updatedToast', { name: entry.title }) })
    await refreshInstalled()
  } catch (e: any) {
    toast.add({ title: t('skillCatalog.actionFailed'), color: 'red' })
  } finally {
    busyKey.value = null
  }
}

onMounted(load)
</script>
