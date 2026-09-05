<template>
  <div class="px-6 py-4">
    <!-- Tabs -->
    <div class="flex items-center gap-4 border-b border-gray-100 dark:border-gray-800 mb-3">
      <button
        v-for="tab in tabs"
        :key="tab.value"
        type="button"
        :data-testid="`skills-tab-${tab.value}`"
        class="relative -mb-px pb-2 text-[12px] font-medium transition-colors border-b-2"
        :class="activeTab === tab.value
          ? 'border-gray-900 dark:border-gray-100 text-gray-900 dark:text-white'
          : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'"
        @click="activeTab = tab.value"
      >
        {{ tab.label }}
        <span class="ms-1 text-[11px] text-gray-400 dark:text-gray-500">{{ tab.count }}</span>
      </button>
    </div>

    <p class="text-xs text-gray-500 dark:text-gray-400 mb-3">
      {{ activeTab === 'catalog' ? $t('skillCatalog.subtitle') : $t('skillCatalog.enabledSubtitle') }}
    </p>

    <div
      v-if="!canManage && activeTab === 'catalog'"
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
    <div v-else-if="rows.length === 0" class="py-10 text-center">
      <p class="text-xs text-gray-500 dark:text-gray-400">
        {{ search ? $t('skillCatalog.noResults') : $t('skillCatalog.enabledEmpty') }}
      </p>
      <button
        v-if="!search && activeTab === 'enabled'"
        type="button"
        data-testid="skills-goto-catalog"
        class="mt-2 text-[11px] font-medium text-blue-600 dark:text-blue-400 hover:underline"
        @click="activeTab = 'catalog'"
      >{{ $t('skillCatalog.browse') }}</button>
    </div>

    <div v-else class="border border-gray-100 dark:border-gray-800 rounded-lg overflow-hidden">
      <div
        v-for="(row, i) in rows"
        :key="row.uid"
        :data-testid="`skill-row-${row.key || row.id}`"
        class="transition-colors"
        :class="i > 0 ? 'border-t border-gray-100 dark:border-gray-800' : ''"
      >
        <div
          class="flex items-start gap-3 px-3 py-2.5 cursor-pointer"
          :class="expanded === row.uid ? 'bg-gray-50 dark:bg-gray-800/50' : 'hover:bg-gray-50/70 dark:hover:bg-gray-800/30'"
          @click="onRowClick(row)"
        >
          <UIcon
            :name="row.instruction_id ? 'i-heroicons-arrow-up-right' : 'i-heroicons-chevron-right'"
            class="w-3 h-3 mt-1 shrink-0 text-gray-300 dark:text-gray-600 transition-transform"
            :class="!row.instruction_id && expanded === row.uid ? 'rotate-90' : (row.instruction_id ? '' : 'rtl:rotate-180')"
          />
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-1.5 flex-wrap">
              <span class="text-[13px] text-gray-800 dark:text-gray-200">{{ row.title }}</span>
              <span
                v-if="row.update_available"
                class="inline-flex items-center px-1.5 h-4 rounded bg-amber-100 dark:bg-amber-500/20 text-amber-700 dark:text-amber-300 text-[10px] font-medium"
              >{{ $t('skillCatalog.updateAvailable') }}</span>
              <span
                v-if="row.is_customized"
                class="text-[10px] text-gray-400 dark:text-gray-500"
                :title="$t('skillCatalog.customizedHint')"
              >{{ $t('skillCatalog.customized') }}</span>
              <span
                v-if="activeTab === 'enabled' && !row.key"
                class="text-[10px] text-gray-400 dark:text-gray-500"
              >{{ $t('skillCatalog.custom') }}</span>
            </div>
            <p class="mt-0.5 text-[11px] text-gray-400 dark:text-gray-500">{{ row.description }}</p>
            <div v-if="expanded === row.uid" class="flex items-center gap-1.5 mt-1.5 flex-wrap">
              <span v-if="row.version" class="text-[10px] text-gray-400 dark:text-gray-500">v{{ row.version }}</span>
              <span v-for="tag in (row.tags || [])" :key="tag" :class="chip">{{ tag }}</span>
              <span
                v-if="row.modes && row.modes.length"
                :class="chip"
                :title="$t('skillCatalog.modeScopedHint')"
              >{{ $t('skillCatalog.modeScoped', { modes: row.modes.join(', ') }) }}</span>
            </div>
          </div>

          <div class="shrink-0 flex items-center gap-1.5" @click.stop>
            <!-- A new shipped version to take, or local edits to discard —
                 without this second case an edited skill has no way back to
                 the shipped text. -->
            <button
              v-if="row.key && row.installed && canManage && (row.update_available || row.is_customized)"
              type="button"
              :data-testid="`skill-resync-${row.key}`"
              :disabled="busyKey === row.key"
              class="h-7 px-2.5 rounded-md text-[11px] font-medium disabled:opacity-50"
              :class="row.update_available
                ? 'text-amber-700 dark:text-amber-400 hover:bg-amber-50 dark:hover:bg-amber-500/10'
                : 'text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800'"
              @click="update(row)"
            >{{ row.update_available ? $t('skillCatalog.update') : $t('skillCatalog.reset') }}</button>
            <Spinner v-if="row.key && busyKey === row.key" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500" />
            <!-- Bound to the server's state, not a local ref: the switch moves
                 when the install actually lands, so it can never show enabled
                 for a skill the agent does not have. A hand-authored skill has
                 no catalog entry to toggle — it is enabled by existing. -->
            <UToggle
              v-else-if="row.key"
              :data-testid="`skill-toggle-${row.key}`"
              :model-value="row.installed"
              :disabled="!canManage"
              size="sm"
              :aria-label="row.installed ? $t('skillCatalog.disable') : $t('skillCatalog.enable')"
              :title="canManage ? '' : $t('skillCatalog.adminOnly')"
              @update:model-value="toggle(row)"
            />
          </div>
        </div>

        <!-- Not installed: preview the shipped playbook before enabling it.
             Once installed the row opens in the instruction editor instead. -->
        <div
          v-if="expanded === row.uid && row.body"
          class="px-3 pb-4 ps-9 border-t border-gray-100 dark:border-gray-800 bg-gray-50/40 dark:bg-gray-800/20"
        >
          <div class="pt-3 prose-instruction">
            <InstructionsInstructionText :text="row.body" :prose="true" :markdown="true" />
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
  (e: 'open-instruction', id: string): void
}>()

const { t } = useI18n()
const toast = useToast()

// Enabling a skill creates a global, org-wide row, so it takes org-level
// manage_instructions — the same authority as a global instruction. Everyone
// else browses read-only.
const canManage = computed(() => useCan('manage_instructions'))

const chip = 'inline-flex items-center px-1.5 h-4 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 text-[10px]'

const activeTab = ref<'enabled' | 'catalog'>('enabled')
const catalog = ref<CatalogEntry[]>([])
// Every kind='skill' instruction in the org — pre-built AND hand-authored, so
// the Enabled tab is the whole picture, not only what came from the catalog.
const installedRows = ref<any[]>([])
const loading = ref(false)
const error = ref('')
const busyKey = ref<string | null>(null)
const search = ref('')
const expanded = ref<string | null>(null)

// `uid` is prefixed per tab so a selection can never collide across the two
// lists (the same skill appears in both, as different rows).
const enabledRows = computed(() => {
  const byKey = new Map(catalog.value.map(c => [c.key, c]))
  return installedRows.value.map((row) => {
    const entry = row.catalog_key ? byKey.get(row.catalog_key) : undefined
    return {
      uid: `i:${row.id}`,
      id: row.id,
      instruction_id: row.id,
      // Only claim a catalog key the catalog still ships. A row installed from
      // an entry that has since been retired keeps working as a skill, but the
      // catalog can no longer manage it — showing a toggle would 404 on click.
      key: entry ? row.catalog_key : null,
      title: row.title || row.preview || '',
      description: row.description || row.preview || '',
      version: row.catalog_version || null,
      modes: row.applicable_modes || [],
      tags: entry?.tags || [],
      body: '',
      installed: true,
      update_available: entry?.update_available || false,
      is_customized: entry?.is_customized || false,
    }
  })
})

const catalogRows = computed(() => catalog.value.map(c => ({
  uid: `c:${c.key}`,
  id: c.key,
  instruction_id: c.instruction_id,
  key: c.key,
  title: c.title,
  description: c.description,
  version: c.version,
  modes: c.modes,
  tags: c.tags,
  // Only an uninstalled entry previews inline; an installed one opens in the
  // editor, where the org's own (possibly edited) copy is the truth.
  body: c.installed ? '' : c.body,
  installed: c.installed,
  update_available: c.update_available,
  is_customized: c.is_customized,
})))

const matches = (s: Record<string, any>) => {
  const q = search.value.trim().toLowerCase()
  if (!q) return true
  return (s.title || '').toLowerCase().includes(q)
    || (s.description || '').toLowerCase().includes(q)
    || (s.tags || []).some((tag: string) => tag.toLowerCase().includes(q))
}

const rows = computed(() =>
  (activeTab.value === 'enabled' ? enabledRows.value : catalogRows.value).filter(matches),
)

const tabs = computed(() => [
  { value: 'enabled' as const, label: t('skillCatalog.tabEnabled'), count: enabledRows.value.length },
  { value: 'catalog' as const, label: t('skillCatalog.tabCatalog'), count: catalog.value.length },
])

watch(activeTab, () => { expanded.value = null })

// An installed skill is a normal instruction, so it opens in the normal editor —
// where it can be read, edited and versioned. An uninstalled catalog entry has
// no instruction yet, so it previews in place instead.
const onRowClick = (row: Record<string, any>) => {
  if (row.instruction_id) { emit('open-instruction', row.instruction_id); return }
  expanded.value = expanded.value === row.uid ? null : row.uid
}

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

// Replace one entry in place so the list does not jump while the user works
// through it.
const applyResult = (result: CatalogEntry) => {
  const i = catalog.value.findIndex(s => s.key === result.key)
  if (i !== -1) catalog.value[i] = { ...catalog.value[i], ...result }
}

const toggle = async (row: Record<string, any>) => {
  if (!row.key || !canManage.value || busyKey.value) return
  busyKey.value = row.key
  const enabling = !row.installed
  const removedId = enabling ? null : row.instruction_id
  try {
    const { data, error: err } = await useMyFetch<CatalogEntry>(
      enabling
        ? `/instructions/skill-catalog/${row.key}/install`
        : `/instructions/skill-catalog/${row.key}`,
      { method: enabling ? 'POST' : 'DELETE' },
    )
    if (err.value) throw err.value
    if (data.value) applyResult(data.value as CatalogEntry)
    toast.add({
      title: enabling
        ? t('skillCatalog.enabledToast', { name: row.title })
        : t('skillCatalog.disabledToast', { name: row.title }),
    })
    await refreshInstalled()
    emit('changed', { removedId })
  } catch (e: any) {
    toast.add({ title: t('skillCatalog.actionFailed'), color: 'red' })
  } finally {
    busyKey.value = null
  }
}

const update = async (row: Record<string, any>) => {
  if (!row.key || !canManage.value || busyKey.value) return
  // Either way this discards the org's copy, so local edits need consent.
  if (row.is_customized && !confirm(t('skillCatalog.confirmOverwrite', { name: row.title }))) return
  busyKey.value = row.key
  try {
    const { data, error: err } = await useMyFetch<CatalogEntry>(
      `/instructions/skill-catalog/${row.key}/update`, { method: 'POST' },
    )
    if (err.value) throw err.value
    if (data.value) applyResult(data.value as CatalogEntry)
    toast.add({ title: t('skillCatalog.updatedToast', { name: row.title }) })
    await refreshInstalled()
    emit('changed', {})
  } catch (e: any) {
    toast.add({ title: t('skillCatalog.actionFailed'), color: 'red' })
  } finally {
    busyKey.value = null
  }
}

onMounted(load)
</script>
