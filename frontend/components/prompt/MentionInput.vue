<template>
  <div class="relative w-full">
    <div
      ref="inputRef"
      contenteditable="true"
      :dir="inputDir"
      :class="[
        'w-full outline-none resize-none bg-transparent text-gray-900 dark:text-white placeholder-gray-400 text-start',
        props.compact ? 'text-sm leading-[20px]' : 'text-sm min-h-[40px]'
      ]"
      :style="{ minHeight: minHeight, maxHeight: maxHeight, overflowY: 'auto' }"
      @input="handleInput"
      @keydown="handleKeydown"
      @paste.prevent="handlePaste"
      @click="handleClick"
    ></div>

    <!-- Dropdown for mentions -->
    <div
      v-if="showDropdown"
      ref="dropdownRef"
      class="absolute z-50 w-80 max-h-80 overflow-y-auto bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-md shadow-md text-start"
      :style="dropdownStyle"
    >
      <!-- Loading state -->
      <div v-if="isLoadingMentions" class="p-2 text-start text-xs text-gray-500 dark:text-gray-400 flex items-center gap-2">
        <Spinner class="w-3 h-3" />
        <span>{{ $t('mentionInput.loading') }}</span>
      </div>

      <!-- Default view: collapsed list of category names (before typing). -->
      <div v-else-if="showCategoryList" class="py-2">
        <div
          v-for="(category, categoryIndex) in categoryList"
          :key="category.name"
          :class="[
            'group px-2 py-1.5 cursor-pointer flex items-center justify-between hover:bg-gray-50 dark:hover:bg-gray-800',
            { 'bg-blue-50 dark:bg-blue-950': selectedIndex === categoryIndex }
          ]"
          :data-idx="categoryIndex"
          @click="enterCategory(category.name)"
        >
          <div class="flex items-center space-x-2 min-w-0">
            <Icon :name="categoryIcon(category.name)" class="w-3.5 h-3.5 flex-shrink-0 text-gray-500 dark:text-gray-400" />
            <span class="text-[12px] text-gray-900 dark:text-white truncate">{{ category.label }}</span>
          </div>
          <Icon name="heroicons-chevron-right" class="w-3.5 h-3.5 text-gray-400 flex-shrink-0" />
        </div>
        <div v-if="categoryList.length === 0" class="px-2 py-4 text-xs text-gray-500 dark:text-gray-400">
          {{ $t('mentionInput.noResults') }}
        </div>
      </div>

      <!-- Search / entered-category item view -->
      <div v-else-if="!expandedItem" class="py-2">
        <!-- Back row when a single category was entered (no active query). -->
        <div
          v-if="activeCategory && !hasQuery"
          class="px-2 py-1 mb-1 cursor-pointer flex items-center gap-1.5 text-[12px] text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800"
          @click="backToCategories"
        >
          <Icon name="heroicons-chevron-left" class="w-3.5 h-3.5 flex-shrink-0" />
          <span>{{ $t('mentionInput.allCategories') }}</span>
        </div>
        <div v-for="(category, categoryIndex) in filteredCategories" :key="category.name">
          <div class="px-2 py-1 text-[12px] font-medium text-gray-500 dark:text-gray-400">{{ category.label }}</div>
          <div v-if="category.items.length === 0" class="px-2 py-2 text-[12px] text-gray-400">{{ $t('mentionInput.noResults') }}</div>
          <div
            v-for="(item, itemIndex) in category.items"
            :key="item.id"
            :class="[
              'group px-2 py-1 flex items-center justify-between hover:bg-gray-50 dark:hover:bg-gray-800',
              item.needs_connect ? 'cursor-default' : 'cursor-pointer',
              { 'bg-blue-50 dark:bg-blue-950': selectedIndex === getCumulativeIndex(categoryIndex, itemIndex) }
            ]"
            :data-idx="getCumulativeIndex(categoryIndex, itemIndex)"
            @click="item.needs_connect ? connectAgent(item) : selectItem(item, category.name)"
          >
            <div :class="['flex items-center space-x-2 flex-1 min-w-0', { 'opacity-50': item.needs_connect }]">
              <DataSourceIcon v-if="category.name === 'data_sources' || category.name === 'tables'" :type="item.icon_type" class="h-3.5 flex-shrink-0" />
              <Icon v-if="category.name === 'tables'" name="heroicons-table-cells" class="w-3.5 h-3.5 flex-shrink-0 text-gray-500 dark:text-gray-400" />
              <Icon v-else-if="category.name === 'files'" name="heroicons-document" class="w-3.5 flex-shrink-0 text-gray-500 dark:text-gray-400" />
              <Icon v-else-if="category.name === 'entities'" :name="item.entity_type === 'metric' ? 'heroicons-chart-bar' : 'heroicons-cube'" class="w-3.5 h-3.5 flex-shrink-0 text-gray-500 dark:text-gray-400" />
              <Icon v-else-if="category.name === 'instructions'" :name="item.kind === 'skill' ? 'heroicons-sparkles' : 'heroicons-book-open'" class="w-3.5 h-3.5 flex-shrink-0 text-gray-500 dark:text-gray-400" />
              <Icon v-else-if="category.name === 'prompts'" name="heroicons-command-line" class="w-3.5 h-3.5 flex-shrink-0 text-gray-500 dark:text-gray-400" />

              <div class="flex flex-col min-w-0 flex-1">
                <span class="text-[12px] text-gray-900 dark:text-white truncate">{{ item.name }}</span>
                <span v-if="category.name === 'tables' && item.subtitle" class="text-[11px] text-gray-400 truncate">{{ item.subtitle }}</span>
              </div>
            </div>

            <!-- Per-agent Connect (sign-in) affordance — mirrors DataSourceSelector. -->
            <button
              v-if="item.needs_connect"
              type="button"
              :disabled="connectingId === item.id"
              class="flex-shrink-0 inline-flex items-center gap-1 px-2 py-0.5 text-[11px] text-blue-600 bg-blue-50 dark:bg-blue-950 border border-blue-200 rounded-md hover:bg-blue-100 dark:hover:bg-blue-900/50 transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
              @click.stop="connectAgent(item)"
            >
              <Spinner v-if="connectingId === item.id" class="w-3 h-3" />
              <Icon v-else name="heroicons-key" class="w-3 h-3" />
              {{ $t('data.connect') }}
            </button>
            <button
              v-else-if="['data_sources', 'tables', 'entities'].includes(category.name)"
              @click.stop="expandItem(item, category.name)"
              class="text-gray-400 hover:text-gray-600 p-0.5 flex-shrink-0 opacity-0 group-hover:opacity-100 transition-opacity"
            >
              <Icon name="heroicons-chevron-right" class="w-3.5 h-3.5" />
            </button>
          </div>
        </div>

        <div v-if="filteredCategories.length === 0" class="px-2 py-4 text-xs text-gray-500 dark:text-gray-400">
          {{ $t('mentionInput.noResults') }}
        </div>
      </div>

      <!-- Expanded item detail view -->
      <div v-else class="p-2">
        <div class="flex items-center justify-between mb-2">
          <div class="flex items-center gap-2 min-w-0">
            <button @click="closeItemCard" class="text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded p-1">
              <Icon name="heroicons-chevron-left" class="w-4 h-4" />
            </button>
            <DataSourceIcon v-if="expandedCategory === 'data_sources' || expandedCategory === 'tables'" :type="expandedItem?.icon_type" class="h-3.5 flex-shrink-0" />
            <Icon v-else-if="expandedCategory === 'files'" name="heroicons-document" class="w-3.5 h-3.5 flex-shrink-0 text-gray-500 dark:text-gray-400" />
            <Icon v-else-if="expandedCategory === 'entities'" :name="expandedItem?.entity_type === 'metric' ? 'heroicons-chart-bar' : 'heroicons-cube'" class="w-3.5 h-3.5 flex-shrink-0 text-gray-500 dark:text-gray-400" />
            <div class="text-[13px] font-medium truncate">{{ expandedItem?.name }}</div>
          </div>
          <button @click="selectItem(expandedItem, expandedCategory)" class="text-sm text-blue-600 hover:text-blue-700 font-medium px-1">+</button>
        </div>

        <!-- Agent details: description + tables + tools -->
        <div v-if="expandedCategory === 'data_sources'" class="space-y-2">
          <div v-if="expandedItem?.description" class="text-[12px] text-gray-600 dark:text-gray-400 leading-snug line-clamp-4">{{ expandedItem.description }}</div>
          <div>
            <div class="text-[11px] text-gray-500 dark:text-gray-400 mb-1">{{ $t('mentionInput.tables') }}</div>
            <div v-if="isLoadingTables" class="px-2 py-2 text-[12px] text-gray-400 flex items-center gap-1">
              <Spinner class="w-3 h-3" />
            </div>
            <div v-else class="max-h-40 overflow-auto border rounded">
              <div
                v-for="t in tablesForExpandedDataSource"
                :key="t.id"
                class="px-2 py-1 text-[12px] flex items-center gap-2 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
                @click="selectItem(t, 'tables')"
              >
                <DataSourceIcon :type="t.icon_type" class="h-3" />
                <span class="truncate flex-1">{{ t.name }}</span>
                <Icon name="heroicons-plus" class="w-3 h-3 flex-shrink-0 text-gray-400" />
              </div>
              <div v-if="tablesForExpandedDataSource.length === 0" class="px-2 py-2 text-[12px] text-gray-400">{{ $t('mentionInput.noTables') }}</div>
            </div>
          </div>
        </div>

        <!-- Table details: connection/data source info + columns list -->
        <div v-else-if="expandedCategory === 'tables'" class="space-y-1">
          <div v-if="expandedItem?.connection_name || expandedItem?.data_source_name" class="flex flex-wrap gap-1 text-[11px] text-gray-500 dark:text-gray-400">
            <span v-if="expandedItem?.connection_name" class="px-1.5 py-0.5 bg-gray-100 dark:bg-gray-800 rounded">{{ expandedItem.connection_name }}</span>
            <span v-if="expandedItem?.data_source_name" class="px-1.5 py-0.5 bg-gray-100 dark:bg-gray-800 rounded">{{ expandedItem.data_source_name }}</span>
          </div>
          <div class="text-[11px] text-gray-500 dark:text-gray-400">{{ $t('mentionInput.columns') }}</div>
          <div class="flex flex-wrap gap-1 max-h-40 overflow-auto">
            <span
              v-for="(col, idx) in (expandedItem?.columns || [])"
              :key="idx"
              class="px-1.5 py-0.5 bg-white dark:bg-gray-900 rounded border text-[11px] text-gray-700 dark:text-gray-300"
            >
              {{ typeof col === 'string' ? col : (col as any).name }}
              <span v-if="typeof col === 'object' && (col as any).dtype" class="text-gray-400 ms-1">({{ (col as any).dtype }})</span>
            </span>
            <span v-if="!(expandedItem?.columns || []).length" class="text-[12px] text-gray-400">{{ $t('mentionInput.noColumns') }}</span>
          </div>
        </div>

        <!-- Entity details: inline description + data preview inside dropdown -->
        <div v-else-if="expandedCategory === 'entities'" class="space-y-2">
          <div v-if="entityLoading" class="text-[11px] text-gray-500 dark:text-gray-400 flex items-center gap-2"><Spinner class="w-3 h-3" /> {{ $t('mentionInput.loading') }}</div>
          <template v-else>
            <div v-if="(entityDetails?.description || expandedItem?.description)" class="text-[11px] text-gray-600 dark:text-gray-400 leading-snug">{{ entityDetails?.description || expandedItem?.description }}</div>
            <div v-if="entityPreviewColumns.length && entityPreviewRows.length" class="overflow-auto border rounded">
              <table class="min-w-full text-[11px]">
                <thead class="bg-gray-50 dark:bg-gray-900 sticky top-0 border-b">
                  <tr>
                    <th v-for="col in entityPreviewColumns" :key="col" class="px-2 py-1 text-start font-medium text-gray-700 dark:text-gray-300">{{ col }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(row, rIdx) in entityPreviewRows" :key="rIdx" class="border-b">
                    <td v-for="col in entityPreviewColumns" :key="col" class="px-2 py-1 text-gray-800 dark:text-gray-200">{{ row[col] }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div v-else class="text-[12px] text-gray-400">{{ $t('mentionInput.noData') }}</div>
            <div class="pt-1">
              <NuxtLink :to="`/queries/${expandedItem?.id}`" class="text-[11px] px-2 py-0.5 rounded border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800">{{ $t('mentionInput.openPage') }}</NuxtLink>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- Parameter fill for a selected prompt (only when it has parameters). -->
    <PromptParametersModal
      v-if="showPromptParams"
      v-model="showPromptParams"
      :prompt="promptForParams"
      @confirm="onPromptParamsConfirm"
      @cancel="cancelPromptParams"
    />

    <!-- Per-agent user credentials / OAuth modal for connecting user_required agents -->
    <UserDataSourceCredentialsModal
      v-model="showCredsModal"
      :data-source="selectedConnectDs"
      @saved="onAgentCredentialsSaved"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import Spinner from '~/components/Spinner.vue'
import PromptParametersModal from '~/components/prompt/PromptParametersModal.vue'
import UserDataSourceCredentialsModal from '~/components/UserDataSourceCredentialsModal.vue'
import { usePermissions, useResourcePermissions } from '~/composables/usePermissions'
import { usePromptFill } from '~/composables/usePromptFill'
import { useDataSourceConnect } from '~/composables/useDataSourceConnect'

const { t, locale: i18nLocale } = useI18n({ useScope: 'global' })

interface MentionItem {
  id: string
  type: 'data_source' | 'datasource_table' | 'file' | 'entity' | 'prompt' | 'instruction'
  name: string
  subtitle?: string
  icon_type?: string
  entity_type?: string
  description?: string
  columns?: string[]
  status?: string
  // Instruction kind: 'instruction' | 'skill' (carried on instruction mentions).
  kind?: string
  data_source_id?: string
  data_source_name?: string
  connection_name?: string
  // Prompt-only fields (carried for "consume into text" behavior).
  text?: string
  parameters?: any[]
  mentions?: { name: string, items: any[] }[]
  // Agent connect (per-agent OAuth sign-in) fields — the raw data source
  // object as returned by /data_sources/active, used by the Connect affordance.
  raw?: any
  needs_connect?: boolean
}

interface MentionCategory {
  name: string
  label: string
  items: MentionItem[]
}

const props = defineProps({
  modelValue: {
    type: String,
    default: ''
  },
  placeholder: {
    type: String,
    default: ''
  },
  rows: {
    type: Number,
    default: 2
  },
  compact: {
    type: Boolean,
    default: false
  },
  categories: {
    type: Array as () => string[],
    default: () => ['data_sources', 'instructions', 'prompts', 'files', 'entities']
  },
  selectedDataSourceIds: {
    type: Array as () => string[],
    default: () => []
  },
  // When set, restricts mentionable data sources (and the tables/entities
  // scoped to them) to those the user has this permission on. Used by
  // AddTestCaseModal so users only mention DSs they can create evals for.
  permission: {
    type: String,
    default: ''
  }
})

const emit = defineEmits(['update:modelValue', 'update:mentions', 'update:mentionsGroups', 'submit'])

const inputRef = ref<HTMLDivElement | null>(null)
const dropdownRef = ref<HTMLDivElement | null>(null)
const textContent = ref('')
const showDropdown = ref(false)
const selectedIndex = ref(0)
const currentMentionStartIndex = ref(-1)
const expandedItem = ref<MentionItem | null>(null)
const expandedCategory = ref<string>('')
// The category the user has "entered" from the default category-list view
// ('' = show the category list). Only relevant while the @-query is empty; as
// soon as the user types, search flattens across all categories regardless.
const activeCategory = ref<string>('')
const detailsCache = ref<Record<string, any>>({})
const entityLoading = ref(false)
const mentions = ref<MentionItem[]>([])
const dropdownPosition = ref({ top: '0px', left: '0px' })
const allCategories = ref<MentionCategory[]>([])
const isLoadingMentions = ref(false)
const orgPermsState = usePermissions()
const resourcePermsState = useResourcePermissions()

// Per-agent connect (sign-in) state — mirrors DataSourceSelector. An agent that
// is user_required and not connected (no creds, no system fallback) shows a
// grayed row with a blue Connect button that triggers the same OAuth / creds
// modal flow as DataSourceSelector.
const { connectingId, needsUserConnection, startConnect, asCredentialsModalSource } = useDataSourceConnect()
const showCredsModal = ref(false)
const selectedConnectDs = ref<any>(null)

async function connectAgent(item: MentionItem) {
  const ds = item.raw || item
  const openModal = await startConnect(ds)
  if (!openModal) return // OAuth redirect in progress — page is navigating away
  selectedConnectDs.value = asCredentialsModalSource(ds)
  showCredsModal.value = true
}

async function onAgentCredentialsSaved() {
  showCredsModal.value = false
  // Re-fetch so a freshly-connected agent moves out of the "needs connect" state.
  await fetchAvailableMentions()
}

// Prompt "consume into text" state. A consumed prompt's mentions are kept in
// `promptMentions` (flat MentionItem list) and merged into the emitted mentions
// — they have no DOM span, so updateMentionsList() would otherwise drop them.
const { substitute, mergeMentions } = usePromptFill()
const showPromptParams = ref(false)
const promptForParams = ref<MentionItem | null>(null)
const promptMentions = ref<MentionItem[]>([])

// While the field is empty, `dir="auto"` has no strong character to scan and
// falls back to LTR — which left-aligns an RTL placeholder (e.g. Hebrew). Fall
// back to the UI locale direction when empty; once the user types, switch to
// `auto` so the direction follows the typed text.
const RTL_LOCALES = new Set(['he', 'ar', 'fa', 'ur'])
const inputDir = computed(() => {
  if (textContent.value.trim().length > 0) return 'auto'
  return RTL_LOCALES.has(String(i18nLocale.value)) ? 'rtl' : 'ltr'
})

const lineHeightPx = computed(() => props.compact ? 18 : 24)
const minHeight = computed(() => `${Math.max(1, props.rows) * lineHeightPx.value}px`)
const maxHeight = computed(() => `${8 * lineHeightPx.value}px`)

// The raw @-query text (after the '@', before the caret). Empty until the user
// types — drives the "category list vs items" default view.
const mentionQuery = computed(() => {
  if (currentMentionStartIndex.value === -1) return ''
  return textContent.value.slice(currentMentionStartIndex.value + 1)
})
const hasQuery = computed(() => mentionQuery.value.trim().length > 0)

// The clickable category rows shown by default (before the user types). Order
// follows the `categories` prop; only categories present in allCategories with
// a resolved label are shown.
const categoryList = computed(() => {
  return props.categories
    .map(name => allCategories.value.find(c => c.name === name))
    .filter((c): c is MentionCategory => !!c)
})

const filteredCategories = computed(() => {
  if (currentMentionStartIndex.value === -1) return []

  const mentionText = mentionQuery.value.toLowerCase()
  const hasSelectedDataSources = props.selectedDataSourceIds.length > 0

  return allCategories.value
    .filter(cat => props.categories.includes(cat.name))
    // While a query is active, search flattens across ALL categories. With no
    // query but a category "entered", restrict to that one category's items.
    .filter(cat => hasQuery.value || !activeCategory.value || cat.name === activeCategory.value)
    .map(category => {
      let items = category.items

      // Permission allowlist (e.g. only DSs the user can create evals for).
      // Uses explicit per-DS grants only (full_admin bypasses).
      if (props.permission) {
        const isAdmin = orgPermsState.value.includes('full_admin_access')
        const allowed = (allCategories.value.find(c => c.name === 'data_sources')?.items || [])
          .filter((ds: any) => {
            if (isAdmin) return true
            const key = `data_source:${ds.id}`
            return resourcePermsState.value[key]?.includes(props.permission) ?? false
          })
          .map((ds: any) => ds.id)
        const allowedSet = new Set(allowed)
        if (category.name === 'data_sources') {
          items = items.filter(item => allowedSet.has(item.id))
        } else if (category.name === 'tables') {
          items = items.filter(item => item.data_source_id && allowedSet.has(item.data_source_id))
        } else if (category.name === 'entities') {
          items = items.filter(item => Array.isArray((item as any).data_source_ids) && (item as any).data_source_ids.some((dsId: string) => allowedSet.has(dsId)))
        }
      }

      // CLIENT-SIDE filtering by selected data sources
      if (hasSelectedDataSources) {
        if (category.name === 'data_sources') {
          // Keep needs-connect agents visible regardless of selection so the
          // user can always reach the Connect affordance (they are never part of
          // the selected set until connected).
          items = items.filter(item => item.needs_connect || props.selectedDataSourceIds.includes(item.id))
        } else if (category.name === 'tables') {
          items = items.filter(item => item.data_source_id && props.selectedDataSourceIds.includes(item.data_source_id))
        } else if (category.name === 'entities') {
          items = items.filter(item => Array.isArray((item as any).data_source_ids) && (item as any).data_source_ids.some((dsId: string) => props.selectedDataSourceIds.includes(dsId)))
        }
      }

      // Filter by search text
      items = items.filter(item =>
        (item.name || '').toLowerCase().includes(mentionText) ||
        (item.subtitle && item.subtitle.toLowerCase().includes(mentionText))
      )
      // Limit to 10 per category
      items = items.slice(0, 10)

      return {
        ...category,
        items
      }
    })
    // Hide empty categories, EXCEPT the one the user explicitly entered (so an
    // entered-but-empty category still shows its header + an empty message).
    .filter(category => category.items.length > 0 || (!hasQuery.value && category.name === activeCategory.value))
})

const dropdownStyle = computed(() => ({
  bottom: '100%',
  left: '0px',
  marginBottom: '8px'
}))

function getCumulativeIndex(categoryIndex: number, itemIndex: number): number {
  let index = 0
  for (let i = 0; i < categoryIndex; i++) {
    index += filteredCategories.value[i].items.length
  }
  return index + itemIndex
}

function getTotalItems() {
  return filteredCategories.value.reduce((total, cat) => total + cat.items.length, 0)
}

function getItemAtIndex(index: number) {
  let currentIndex = 0
  for (const category of filteredCategories.value) {
    if (index < currentIndex + category.items.length) {
      return { item: category.items[index - currentIndex], category: category.name }
    }
    currentIndex += category.items.length
  }
  return null
}

// True when the dropdown should show the collapsed category-name list (default,
// pre-typing state with no category entered and no item expanded).
const showCategoryList = computed(() =>
  !hasQuery.value && !activeCategory.value && !expandedItem.value
)

// Icon shown next to each category name in the collapsed list. Mirrors the
// per-item icons used in the item view.
function categoryIcon(name: string): string {
  switch (name) {
    case 'data_sources': return 'heroicons-cube-transparent'
    case 'instructions': return 'heroicons-book-open'
    case 'prompts': return 'heroicons-command-line'
    case 'files': return 'heroicons-document'
    case 'entities': return 'heroicons-chart-bar'
    default: return 'heroicons-tag'
  }
}

// Enter a category from the default list: show its items. Data is already
// loaded on mount; ensureCategoryLoaded re-fetches lazily if a category is
// somehow empty (e.g. a prior fetch failed), so re-entry self-heals.
function enterCategory(name: string) {
  activeCategory.value = name
  selectedIndex.value = 0
  ensureCategoryLoaded(name)
}

// Return from a category's item list back to the category-name list.
function backToCategories() {
  activeCategory.value = ''
  selectedIndex.value = 0
}

function ensureCategoryLoaded(name: string) {
  const cat = allCategories.value.find(c => c.name === name)
  if (cat && cat.items.length > 0) return
  if (name === 'data_sources' || name === 'files' || name === 'entities') {
    fetchAvailableMentions()
  } else if (name === 'prompts') {
    fetchPromptMentions()
  } else if (name === 'instructions') {
    fetchInstructionMentions()
  }
}

function getCaretPosition(element: HTMLElement): number {
  const selection = window.getSelection()
  if (selection && selection.rangeCount > 0) {
    const range = selection.getRangeAt(0)
    const preCaretRange = range.cloneRange()
    preCaretRange.selectNodeContents(element)
    preCaretRange.setEnd(range.endContainer, range.endOffset)
    return preCaretRange.toString().length
  }
  return 0
}

// Find the last @ character that is NOT inside a .mention span
// Returns the position in the flattened text (innerText), or -1 if not found
function findLastAtOutsideMentions(element: HTMLElement): number {
  let lastAtIndex = -1
  let currentPos = 0

  function traverse(node: Node) {
    if (node.nodeType === Node.TEXT_NODE) {
      const text = node.textContent || ''
      // Check if this text node is inside a mention
      const isInsideMention = (node.parentElement?.classList.contains('mention') ||
                               node.parentElement?.closest('.mention'))

      if (!isInsideMention) {
        // Find all @ in this text node (search from end to get last one)
        for (let i = text.length - 1; i >= 0; i--) {
          if (text[i] === '@') {
            const absolutePos = currentPos + i
            if (absolutePos > lastAtIndex) {
              lastAtIndex = absolutePos
            }
          }
        }
      }
      currentPos += text.length
    } else if (node.nodeType === Node.ELEMENT_NODE) {
      // For mention spans, just add their text length but don't search inside
      if ((node as HTMLElement).classList.contains('mention')) {
        currentPos += node.textContent?.length || 0
      } else {
        // Traverse children
        for (const child of Array.from(node.childNodes)) {
          traverse(child)
        }
      }
    }
  }

  traverse(element)
  return lastAtIndex
}

function setCaretPosition(element: HTMLElement, position: number) {
  const range = document.createRange()
  const sel = window.getSelection()

  let currentPos = 0
  let found = false

  function searchNode(node: Node): boolean {
    if (node.nodeType === Node.TEXT_NODE) {
      const nodeLength = node.textContent?.length || 0
      if (currentPos + nodeLength >= position) {
        range.setStart(node, position - currentPos)
        range.collapse(true)
        found = true
        return true
      }
      currentPos += nodeLength
    } else if (node.nodeType === Node.ELEMENT_NODE && (node as HTMLElement).classList.contains('mention')) {
      const nodeLength = node.textContent?.length || 0
      if (currentPos + nodeLength >= position) {
        // If we're in a mention, place cursor after it
        range.setStartAfter(node)
        range.collapse(true)
        found = true
        return true
      }
      currentPos += nodeLength
    } else {
      for (const child of Array.from(node.childNodes)) {
        if (searchNode(child)) return true
      }
    }
    return false
  }

  searchNode(element)

  if (found && sel) {
    sel.removeAllRanges()
    sel.addRange(range)
  }
}

function handleInput(event: Event) {
  const target = event.target as HTMLDivElement

  // Preserve mention nodes - ensure they don't get broken
  const mentionNodes = target.querySelectorAll('.mention')
  mentionNodes.forEach(node => {
    if (node.childNodes.length > 1 || (node.childNodes[0] && node.childNodes[0].nodeType !== Node.TEXT_NODE)) {
      const mentionText = node.getAttribute('data-mention-id')
      if (mentionText) {
        node.textContent = node.textContent || `@${mentionText}`
      }
    }
  })

  textContent.value = target.innerText

  const cursorPosition = getCaretPosition(target)

  // Find the last @ that is NOT inside a mention span
  const lastAtIndex = findLastAtOutsideMentions(target)

  // Only consider @ characters that are before the cursor
  if (lastAtIndex !== -1 && lastAtIndex < cursorPosition) {
    const textAfterAt = textContent.value.slice(lastAtIndex + 1, cursorPosition)

    // Check if we're typing a mention (@ followed by text without space)
    if (!textAfterAt.includes(' ')) {
      // Make sure we're not inside an existing mention
      const selection = window.getSelection()
      if (selection && selection.rangeCount > 0) {
        const range = selection.getRangeAt(0)
        const container = range.startContainer
        const isInsideMention = container.parentElement?.classList.contains('mention') ||
                               container.parentElement?.closest('.mention')

        if (!isInsideMention) {
          // A freshly-opened mention (new '@') starts at the category list.
          if (currentMentionStartIndex.value !== lastAtIndex) activeCategory.value = ''
          currentMentionStartIndex.value = lastAtIndex
          showDropdown.value = true
          selectedIndex.value = 0
        } else {
          showDropdown.value = false
          currentMentionStartIndex.value = -1
          activeCategory.value = ''
        }
      }
    } else {
      showDropdown.value = false
      currentMentionStartIndex.value = -1
      activeCategory.value = ''
    }
  } else {
    showDropdown.value = false
    currentMentionStartIndex.value = -1
    activeCategory.value = ''
  }

  emit('update:modelValue', textContent.value)
  updateMentionsList()
}

function handleKeydown(event: KeyboardEvent) {
  if (showDropdown.value && showCategoryList.value) {
    // Category-name list navigation (default pre-typing view).
    const total = categoryList.value.length
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault()
        if (total) selectedIndex.value = (selectedIndex.value + 1) % total
        scrollSelectedIntoView()
        return
      case 'ArrowUp':
        event.preventDefault()
        if (total) selectedIndex.value = (selectedIndex.value - 1 + total) % total
        scrollSelectedIntoView()
        return
      case 'Enter':
      case 'ArrowRight':
        event.preventDefault()
        const cat = categoryList.value[selectedIndex.value]
        if (cat) enterCategory(cat.name)
        return
      case 'Escape':
        event.preventDefault()
        showDropdown.value = false
        return
    }
  } else if (showDropdown.value) {
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault()
        selectedIndex.value = (selectedIndex.value + 1) % getTotalItems()
        scrollSelectedIntoView()
        break
      case 'ArrowUp':
        event.preventDefault()
        selectedIndex.value = (selectedIndex.value - 1 + getTotalItems()) % getTotalItems()
        scrollSelectedIntoView()
        break
      case 'Enter':
        event.preventDefault()
        const selected = getItemAtIndex(selectedIndex.value)
        if (selected) {
          selectItem(selected.item, selected.category)
        }
        break
      case 'ArrowRight':
        event.preventDefault()
        const toExpand = getItemAtIndex(selectedIndex.value)
        if (toExpand && ['data_sources', 'tables', 'entities'].includes(toExpand.category)) {
          expandItem(toExpand.item, toExpand.category)
        }
        break
      case 'ArrowLeft':
        if (expandedItem.value) {
          event.preventDefault()
          closeItemCard()
        } else if (activeCategory.value && !hasQuery.value) {
          // Back out of an entered category to the category list.
          event.preventDefault()
          backToCategories()
        }
        break
      case 'Escape':
        event.preventDefault()
        if (expandedItem.value) {
          closeItemCard()
        } else if (activeCategory.value && !hasQuery.value) {
          backToCategories()
        } else {
          showDropdown.value = false
        }
        break
    }
  } else {
    // When dropdown is not shown, handle Enter to submit (without Shift)
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      emit('submit')
    }
  }

  // Backspace inside an entered category with an empty query returns to the
  // category list (instead of deleting the '@' that opened the menu).
  if (event.key === 'Backspace' && showDropdown.value && activeCategory.value && !hasQuery.value) {
    event.preventDefault()
    backToCategories()
    return
  }

  // Prevent typing inside mentions
  const selection = window.getSelection()
  if (selection && selection.rangeCount > 0) {
    const range = selection.getRangeAt(0)
    const container = range.startContainer
    const mentionElement = container.parentElement?.closest('.mention')

    // If we're inside a mention and trying to type a character, prevent it
    if (mentionElement && event.key.length === 1) {
      event.preventDefault()
      return
    }
  }

  // Handle backspace/delete on mentions
  if (event.key === 'Backspace' || event.key === 'Delete') {
    const selection = window.getSelection()
    if (selection && selection.rangeCount > 0) {
      const range = selection.getRangeAt(0)
      const node = range.startContainer

      // Check if we're inside a mention (cursor is within the mention span)
      const mentionElement = node.parentElement?.closest('.mention')
      if (mentionElement) {
        event.preventDefault()
        mentionElement.remove()
        textContent.value = inputRef.value?.innerText || ''
        emit('update:modelValue', textContent.value)
        updateMentionsList()
        return
      }

      // Check if cursor is immediately after a mention (for backspace)
      // Only delete mention if: cursor is collapsed AND at position 0 in the text node
      if (event.key === 'Backspace' && range.collapsed && range.startOffset === 0) {
        // Check if previous sibling is a mention
        if (node.previousSibling?.nodeType === Node.ELEMENT_NODE &&
            (node.previousSibling as HTMLElement).classList.contains('mention')) {
          event.preventDefault()
          node.previousSibling.remove()
          textContent.value = inputRef.value?.innerText || ''
          emit('update:modelValue', textContent.value)
          updateMentionsList()
          return
        }
        // Also check if the node itself is right after a mention (when node is the inputRef)
        if (node.nodeType === Node.ELEMENT_NODE) {
          const lastChild = (node as HTMLElement).lastChild
          if (lastChild?.nodeType === Node.ELEMENT_NODE &&
              (lastChild as HTMLElement).classList.contains('mention')) {
            event.preventDefault()
            lastChild.remove()
            textContent.value = inputRef.value?.innerText || ''
            emit('update:modelValue', textContent.value)
            updateMentionsList()
            return
          }
        }
      }

      // Check if cursor is immediately before a mention (for delete key)
      // Only delete mention if: cursor is collapsed AND at end of the text node
      if (event.key === 'Delete' && range.collapsed) {
        const nodeLength = node.textContent?.length || 0
        if (range.startOffset === nodeLength &&
            node.nextSibling?.nodeType === Node.ELEMENT_NODE &&
            (node.nextSibling as HTMLElement).classList.contains('mention')) {
          event.preventDefault()
          node.nextSibling.remove()
          textContent.value = inputRef.value?.innerText || ''
          emit('update:modelValue', textContent.value)
          updateMentionsList()
          return
        }
      }
    }
  }
}

function handleClick(event: MouseEvent) {
  // If clicking on a mention, select the entire mention
  const target = event.target as HTMLElement
  if (target.classList.contains('mention')) {
    event.preventDefault()
    const range = document.createRange()
    range.selectNode(target)
    const selection = window.getSelection()
    selection?.removeAllRanges()
    selection?.addRange(range)
  }
}

// Convert HTML to plain text with markdown-style formatting
function htmlToPlainText(html: string): string {
  const parser = new DOMParser()
  const doc = parser.parseFromString(html, 'text/html')

  function processNode(node: Node, listContext: { type: 'ul' | 'ol', index: number } | null = null): string {
    if (node.nodeType === Node.TEXT_NODE) {
      return node.textContent || ''
    }

    if (node.nodeType !== Node.ELEMENT_NODE) {
      return ''
    }

    const el = node as HTMLElement
    const tag = el.tagName.toLowerCase()

    // Process children
    const processChildren = (ctx: { type: 'ul' | 'ol', index: number } | null = listContext) => {
      return Array.from(el.childNodes).map(child => processNode(child, ctx)).join('')
    }

    switch (tag) {
      case 'br':
        return '\n'
      case 'p':
      case 'div':
        const pContent = processChildren()
        return pContent ? pContent + '\n' : ''
      case 'ul':
        return Array.from(el.children).map(child => processNode(child, { type: 'ul', index: 0 })).join('') + '\n'
      case 'ol':
        let olIndex = 0
        return Array.from(el.children).map(child => {
          olIndex++
          return processNode(child, { type: 'ol', index: olIndex })
        }).join('') + '\n'
      case 'li':
        const liContent = processChildren(null).trim()
        if (listContext?.type === 'ol') {
          return `${listContext.index}. ${liContent}\n`
        }
        return `• ${liContent}\n`
      case 'strong':
      case 'b':
        return processChildren()
      case 'em':
      case 'i':
        return processChildren()
      case 'code':
        return '`' + processChildren() + '`'
      case 'pre':
        return '\n' + processChildren() + '\n'
      case 'h1':
      case 'h2':
      case 'h3':
      case 'h4':
      case 'h5':
      case 'h6':
        return processChildren() + '\n'
      default:
        return processChildren()
    }
  }

  const result = processNode(doc.body)
  // Clean up excessive newlines
  return result.replace(/\n{3,}/g, '\n\n').trim()
}

function handlePaste(event: ClipboardEvent) {
  const html = event.clipboardData?.getData('text/html')
  const plain = event.clipboardData?.getData('text/plain') || ''

  // Use HTML conversion if available and contains list elements, otherwise use plain text
  let text = plain
  if (html && (html.includes('<li') || html.includes('<ol') || html.includes('<ul'))) {
    text = htmlToPlainText(html)
  }

  const selection = window.getSelection()
  if (selection && selection.rangeCount > 0) {
    const range = selection.getRangeAt(0)
    range.deleteContents()
    range.insertNode(document.createTextNode(text))
    range.collapse(false)
    selection.removeAllRanges()
    selection.addRange(range)
  }
  handleInput({ target: inputRef.value } as Event)
}

function expandItem(item: MentionItem, category: string) {
  expandedItem.value = item
  expandedCategory.value = category
  if (category === 'entities' && item?.id) {
    loadEntityInline(String(item.id))
  } else if (category === 'data_sources' && item?.id) {
    // Drill into the agent: fetch its tables via the schema endpoint.
    loadTablesForAgent(String(item.id))
  }
}

function closeItemCard() {
  expandedItem.value = null
  expandedCategory.value = ''
}

function selectItem(item: MentionItem, category: string) {
  // Prompts are not inserted as a chip — they are consumed into plain text.
  if (item.type === 'prompt') {
    if (item.parameters && item.parameters.length) {
      promptForParams.value = item
      showPromptParams.value = true
      // Keep the dropdown closed while the modal is up; @query is replaced on confirm.
      showDropdown.value = false
      return
    }
    insertPromptText(item, item.text || '')
    return
  }

  // Everything else (data sources, tables, files, entities, instructions/skills)
  // is inserted as a normal mention chip that flows through buildMentionGroups
  // → completion mentions. An @-mentioned instruction is force-included into the
  // prompt context server-side (mirrors the FILE mention path).
  if (currentMentionStartIndex.value !== -1 && inputRef.value) {
    const selection = window.getSelection()
    if (!selection || selection.rangeCount === 0) return

    // Create the mention node
    const mentionNode = document.createElement('span')
    mentionNode.className = 'mention'
    mentionNode.setAttribute('contenteditable', 'false')
    mentionNode.setAttribute('data-mention-id', item.id)
    mentionNode.setAttribute('data-mention-type', item.type)
    const dsLabel = item.type === 'datasource_table' ? (item.connection_name || item.data_source_name) : null
    mentionNode.textContent = dsLabel ? `@${dsLabel} / ${item.name}` : `@${item.name}`

    // Find the text node and position where @ starts
    const walker = document.createTreeWalker(
      inputRef.value,
      NodeFilter.SHOW_TEXT,
      null
    )

    let currentPos = 0
    let targetNode: Node | null = null
    let offsetInNode = 0

    while (walker.nextNode()) {
      const node = walker.currentNode
      const nodeLength = node.textContent?.length || 0

      if (currentPos + nodeLength > currentMentionStartIndex.value) {
        targetNode = node
        offsetInNode = currentMentionStartIndex.value - currentPos
        break
      }
      currentPos += nodeLength
    }

    if (!targetNode) {
      // Fallback: couldn't find the text node, bail out
      console.warn('Could not find text node for mention insertion')
      return
    }

    // Calculate how much text to delete (the @ and any search text)
    const currentCursorPos = getCaretPosition(inputRef.value)
    const lengthToDelete = currentCursorPos - currentMentionStartIndex.value

    // Split the text node at the @ position
    const textNode = targetNode as Text
    const beforeText = textNode.textContent?.slice(0, offsetInNode) || ''
    const afterText = textNode.textContent?.slice(offsetInNode + lengthToDelete) || ''

    // Create a document fragment to hold the new content
    const fragment = document.createDocumentFragment()

    if (beforeText) {
      fragment.appendChild(document.createTextNode(beforeText))
    }

    fragment.appendChild(mentionNode)
    fragment.appendChild(document.createTextNode(' '))

    if (afterText) {
      fragment.appendChild(document.createTextNode(afterText))
    }

    // Replace the text node with our fragment
    textNode.parentNode?.replaceChild(fragment, textNode)

    // Set cursor after the mention and space
    const range = document.createRange()
    const spaceNode = mentionNode.nextSibling
    if (spaceNode) {
      range.setStartAfter(spaceNode)
      range.collapse(true)
      selection.removeAllRanges()
      selection.addRange(range)
    }

    // Update state
    textContent.value = inputRef.value.innerText
    emit('update:modelValue', textContent.value)

    currentMentionStartIndex.value = -1
    showDropdown.value = false
    expandedItem.value = null
    activeCategory.value = ''
    selectedIndex.value = 0

    updateMentionsList()
  }
}

// Replace the active "@query" with the prompt's (substituted) plain text and
// merge the prompt's mentions into the box state. No mention span is created.
function insertPromptText(item: MentionItem, plainText: string) {
  if (!inputRef.value) return

  if (currentMentionStartIndex.value !== -1) {
    const selection = window.getSelection()
    if (selection && selection.rangeCount > 0) {
      // Find the text node holding the @ and the offset of the @ within it.
      const walker = document.createTreeWalker(inputRef.value, NodeFilter.SHOW_TEXT, null)
      let currentPos = 0
      let targetNode: Text | null = null
      let offsetInNode = 0
      while (walker.nextNode()) {
        const node = walker.currentNode as Text
        const nodeLength = node.textContent?.length || 0
        if (currentPos + nodeLength > currentMentionStartIndex.value) {
          targetNode = node
          offsetInNode = currentMentionStartIndex.value - currentPos
          break
        }
        currentPos += nodeLength
      }

      if (targetNode) {
        const cursorPos = getCaretPosition(inputRef.value)
        const lengthToDelete = cursorPos - currentMentionStartIndex.value
        const before = targetNode.textContent?.slice(0, offsetInNode) || ''
        const after = targetNode.textContent?.slice(offsetInNode + lengthToDelete) || ''

        const textNode = document.createTextNode(plainText)
        const fragment = document.createDocumentFragment()
        if (before) fragment.appendChild(document.createTextNode(before))
        fragment.appendChild(textNode)
        if (after) fragment.appendChild(document.createTextNode(after))
        targetNode.parentNode?.replaceChild(fragment, targetNode)

        const range = document.createRange()
        range.setStartAfter(textNode)
        range.collapse(true)
        selection.removeAllRanges()
        selection.addRange(range)
      }
    }
  } else {
    // No active @ (e.g. inserted programmatically) — append at the end.
    inputRef.value.appendChild(document.createTextNode(plainText))
  }

  // Merge the prompt's mentions; existing (user) mentions win on id collision.
  mergePromptMentionsIntoState(item.mentions || [])

  textContent.value = inputRef.value.innerText
  emit('update:modelValue', textContent.value)

  currentMentionStartIndex.value = -1
  showDropdown.value = false
  expandedItem.value = null
  activeCategory.value = ''
  selectedIndex.value = 0

  updateMentionsList()
}

// Flatten a prompt's PromptSchema mention groups into MentionItems and merge
// them (deduped by id) into promptMentions, never overriding existing mentions.
function mergePromptMentionsIntoState(groups: { name: string, items: any[] }[]) {
  const existingIds = new Set([
    ...currentDomMentions().map(m => String(m.id)),
    ...promptMentions.value.map(m => String(m.id)),
  ])
  const GROUP_TO_TYPE: Record<string, MentionItem['type']> = {
    'DATA SOURCES': 'data_source',
    'TABLES': 'datasource_table',
    'FILES': 'file',
    'ENTITIES': 'entity',
  }
  for (const g of groups || []) {
    const type = GROUP_TO_TYPE[g.name]
    if (!type) continue
    for (const it of g.items || []) {
      const id = String(it?.id)
      if (!id || existingIds.has(id)) continue
      existingIds.add(id)
      promptMentions.value.push({
        id,
        type,
        name: it.name || it.title || it.filename || id,
        data_source_id: it.datasource_id || it.data_source_id,
        data_source_name: it.data_source_name,
        entity_type: it.entity_type,
      })
    }
  }
}

// Resolve a mention chip back to its full MentionItem by id. Searches the
// top-level categories AND the per-agent table cache (drilled-in tables are not
// part of allCategories), so a table chip inserted from an agent's drill view
// still flows into the emitted mentions.
function findMentionItemById(id: string | null): MentionItem | undefined {
  if (!id) return undefined
  for (const category of allCategories.value) {
    const found = category.items.find(i => i.id === id)
    if (found) return found
  }
  for (const dsId of Object.keys(tablesByAgent.value)) {
    const found = tablesByAgent.value[dsId].find(i => i.id === id)
    if (found) return found
  }
  return undefined
}

function currentDomMentions(): MentionItem[] {
  if (!inputRef.value) return []
  const out: MentionItem[] = []
  inputRef.value.querySelectorAll('.mention').forEach(node => {
    const found = findMentionItemById(node.getAttribute('data-mention-id'))
    if (found) out.push(found)
  })
  return out
}

function onPromptParamsConfirm(values: Record<string, any>) {
  const item = promptForParams.value
  showPromptParams.value = false
  if (!item) return
  insertPromptText(item, substitute(item.text || '', values))
  promptForParams.value = null
}

function cancelPromptParams() {
  showPromptParams.value = false
  promptForParams.value = null
}

// Tables for the currently-expanded agent, lazily fetched from the schema
// endpoint and cached per agent id.
const tablesByAgent = ref<Record<string, MentionItem[]>>({})
const isLoadingTables = ref(false)

const tablesForExpandedDataSource = computed(() => {
  if (!expandedItem.value || expandedCategory.value !== 'data_sources') return [] as any[]
  const dsId = String(expandedItem.value.id)
  return (tablesByAgent.value[dsId] || []).slice(0, 50)
})

async function loadTablesForAgent(dsId: string) {
  if (tablesByAgent.value[dsId] !== undefined) return
  isLoadingTables.value = true
  try {
    const { data, error } = await useMyFetch(`/api/data_sources/${dsId}/schema`, { method: 'GET' })
    if (!error.value && Array.isArray(data.value)) {
      const agent = (allCategories.value.find(c => c.name === 'data_sources')?.items || [])
        .find((d: any) => String(d.id) === dsId) as any
      tablesByAgent.value[dsId] = (data.value as any[]).map((t: any) => ({
        id: String(t.id),
        type: 'datasource_table' as const,
        name: t.name,
        data_source_id: dsId,
        data_source_name: agent?.name,
        connection_name: t.connection_name || agent?.name,
        columns: t.columns || [],
        icon_type: t.connection_type || agent?.icon_type,
      }))
    } else {
      tablesByAgent.value[dsId] = []
    }
  } catch {
    tablesByAgent.value[dsId] = []
  }
  isLoadingTables.value = false
}

const entityDetails = computed(() => {
  const id = expandedItem.value?.id
  if (!id) return null
  return detailsCache.value[id] || { title: expandedItem.value?.name, description: expandedItem.value?.description }
})

async function loadEntityInline(id: string) {
  if (detailsCache.value[id]) return
  entityLoading.value = true
  try {
    const { data, error } = await useMyFetch(`/api/entities/${id}`, { method: 'GET' })
    if (!error.value && data.value) {
      detailsCache.value[id] = data.value
    }
  } catch {}
  entityLoading.value = false
}

const entityPreviewColumns = computed<string[]>(() => {
  const d = entityDetails.value as any
  if (!d) return []
  if (Array.isArray(d?.data?.columns) && d.data.columns.length) {
    return d.data.columns.map((c: any) => c.field || c.headerName || c.name || c)
  }
  const rows = d?.data?.rows
  if (Array.isArray(rows) && rows[0]) return Object.keys(rows[0])
  return []
})

const entityPreviewRows = computed<any[]>(() => {
  const d = entityDetails.value as any
  const rows = d?.data?.rows
  if (Array.isArray(rows)) return rows.slice(0, 20)
  return []
})

function updateMentionsList() {
  if (!inputRef.value) return

  const mentionNodes = inputRef.value.querySelectorAll('.mention')
  const newMentions: MentionItem[] = []

  mentionNodes.forEach(node => {
    const id = node.getAttribute('data-mention-id')
    const found = findMentionItemById(id)
    if (found) newMentions.push(found)
  })

  // Fold in mentions carried by consumed prompts (no DOM span). DOM mentions
  // win on id collision, matching the "current wins" merge contract.
  const domIds = new Set(newMentions.map(m => String(m.id)))
  for (const pm of promptMentions.value) {
    if (!domIds.has(String(pm.id))) newMentions.push(pm)
  }

  mentions.value = newMentions
  emit('update:mentions', newMentions)
  emit('update:mentionsGroups', buildMentionGroups(newMentions))
}

function buildMentionGroups(selected: MentionItem[]) {
  const groups: { name: string, items: any[] }[] = []
  const files: any[] = []
  const dataSources: any[] = []
  const tables: any[] = []
  const entities: any[] = []
  const instructions: any[] = []

  for (const m of selected) {
    if (m.type === 'file') {
      files.push({ id: m.id, filename: m.name })
    } else if (m.type === 'data_source') {
      dataSources.push({ id: m.id, name: m.name })
    } else if (m.type === 'datasource_table') {
      tables.push({ id: m.id, name: m.name, datasource_id: m.data_source_id, data_source_name: m.data_source_name })
    } else if (m.type === 'entity') {
      entities.push({ id: m.id, title: m.name, entity_type: m.entity_type })
    } else if (m.type === 'instruction') {
      instructions.push({ id: m.id, name: m.name, kind: m.kind })
    }
  }

  if (files.length) groups.push({ name: 'FILES', items: files })
  if (dataSources.length) groups.push({ name: 'DATA SOURCES', items: dataSources })
  if (tables.length) groups.push({ name: 'TABLES', items: tables })
  if (entities.length) groups.push({ name: 'ENTITIES', items: entities })
  if (instructions.length) groups.push({ name: 'INSTRUCTIONS', items: instructions })

  return groups
}

function setPlaceholder() {
  if (inputRef.value && inputRef.value.innerText.trim() === '') {
    inputRef.value.setAttribute('data-placeholder', props.placeholder || t('mentionInput.placeholder'))
  }
}

watch(i18nLocale, () => setPlaceholder())

// Helper to format time ago
function formatTimeAgo(dateStr: string | null): string {
  if (!dateStr) return ''
  try {
    const date = new Date(dateStr)
    const now = new Date()
    const seconds = Math.floor((now.getTime() - date.getTime()) / 1000)

    if (seconds < 60) return t('mentionInput.time.justNow')
    if (seconds < 3600) return t('mentionInput.time.minutesAgo', { n: Math.floor(seconds / 60) })
    if (seconds < 86400) return t('mentionInput.time.hoursAgo', { n: Math.floor(seconds / 3600) })
    if (seconds < 604800) return t('mentionInput.time.daysAgo', { n: Math.floor(seconds / 86400) })
    if (seconds < 2592000) return t('mentionInput.time.weeksAgo', { n: Math.floor(seconds / 604800) })
    return t('mentionInput.time.monthsAgo', { n: Math.floor(seconds / 2592000) })
  } catch {
    return ''
  }
}

// Per-category item caches. Each category is sourced from its own dedicated
// endpoint so a single failing endpoint never blanks the whole menu. The menu
// is assembled (in display order) by rebuildCategories().
const agentCategoryItems = ref<MentionItem[]>([])
// Instructions AND skills are listed together in one 'instructions' category
// (kind carried per item); they are sourced from GET /instructions (no kind
// filter).
const instructionCategoryItems = ref<MentionItem[]>([])
const fileCategoryItems = ref<MentionItem[]>([])
const entityCategoryItems = ref<MentionItem[]>([])
// Saved prompts are a separate API (GET /prompts) mapped into the 'prompts'
// category. Selecting one substitutes its text into the box.
const promptCategoryItems = ref<MentionItem[]>([])

// Assemble the top-level mention categories in display order:
// Agents, Instructions, Prompts, Files, Queries. The (hidden) 'tables'
// category is only reachable by drilling into an agent.
function rebuildCategories() {
  allCategories.value = [
    { name: 'data_sources', label: t('mentionInput.categories.dataSources'), items: agentCategoryItems.value },
    { name: 'instructions', label: t('mentionInput.categories.instructions'), items: instructionCategoryItems.value },
    { name: 'prompts', label: t('mentionInput.categories.prompts'), items: promptCategoryItems.value },
    { name: 'files', label: t('mentionInput.categories.files'), items: fileCategoryItems.value },
    { name: 'entities', label: t('mentionInput.categories.queries'), items: entityCategoryItems.value },
  ]
}

// Fetch agents, files and queries (entities). Agents come from the same source
// DataSourceSelector uses (/data_sources/active?include_unconnected=true) so
// unconnected user_required agents surface with a Connect affordance. Files use
// the dedicated /files endpoint (the /mentions/available endpoint can 500), and
// queries (entities) keep their existing /mentions/available source.
async function fetchAvailableMentions() {
  if (isLoadingMentions.value) return
  isLoadingMentions.value = true

  // Each category is fetched independently so one failing/forbidden endpoint
  // (e.g. /files 403 for users without manage_files) never blanks the menu.
  const tasks = [
    (async () => {
      try {
        const { data, error } = await useMyFetch('/data_sources/active', { method: 'GET', query: { include_unconnected: true } })
        if (!error.value && Array.isArray(data.value)) {
          agentCategoryItems.value = (data.value as any[]).map((ds: any) => ({
            id: String(ds.id),
            type: 'data_source' as const,
            name: ds.name,
            description: ds.description,
            subtitle: ds.description || ds.type,
            icon_type: ds.type,
            // Per-agent connect state — mirrors DataSourceSelector's needs-connect logic.
            needs_connect: needsUserConnection(ds),
            raw: ds,
          }))
        }
      } catch { /* keep prior items */ }
    })(),
    (async () => {
      try {
        const { data, error } = await useMyFetch('/files', { method: 'GET' })
        if (!error.value && Array.isArray(data.value)) {
          fileCategoryItems.value = (data.value as any[]).map((file: any) => ({
            id: String(file.id),
            type: 'file' as const,
            name: file.filename,
            subtitle: formatTimeAgo(file.created_at),
          }))
        }
      } catch { /* keep prior items */ }
    })(),
    (async () => {
      try {
        const { data, error } = await useMyFetch('/mentions/available?categories=entities', { method: 'GET' })
        if (!error.value && data.value) {
          const apiData = data.value as any
          entityCategoryItems.value = (apiData.entities || []).map((entity: any) => ({
            ...entity,
            id: String(entity.id),
            type: 'entity' as const,
            name: entity.title,
            subtitle: entity.entity_type,
          }))
        }
      } catch { /* keep prior items */ }
    })(),
  ]

  await Promise.allSettled(tasks)
  rebuildCategories()
  isLoadingMentions.value = false
}

// Instructions and skills are listed together (one category). Sourced from
// GET /instructions (no kind filter — both kinds returned); kind is kept per
// item. Selecting one inserts a mention chip that force-includes the
// instruction's content into context server-side (mirrors the FILE path).
async function fetchInstructionMentions() {
  try {
    const { data, error } = await useMyFetch('/instructions?limit=50', { method: 'GET' })
    if (error.value) { instructionCategoryItems.value = []; return }
    const list = (data.value as any)?.items || []
    instructionCategoryItems.value = list.map((ins: any) => ({
      id: String(ins.id),
      type: 'instruction' as const,
      name: ins.title || (ins.text || '').slice(0, 60),
      kind: ins.kind || 'instruction',
    } as MentionItem))
  } catch {
    instructionCategoryItems.value = []
  }
  rebuildCategories()
}

async function fetchPromptMentions() {
  try {
    const { data, error } = await useMyFetch('/prompts?limit=50', { method: 'GET' })
    if (error.value) { promptCategoryItems.value = []; return }
    const list = (data.value as any)?.prompts || []
    promptCategoryItems.value = list.map((p: any) => ({
      id: String(p.id),
      type: 'prompt' as const,
      name: p.title || (p.text || '').slice(0, 60),
      subtitle: (p.parameters && p.parameters.length) ? t('mentionInput.categories.prompts') : undefined,
      text: p.text || '',
      parameters: p.parameters || [],
      mentions: p.mentions || [],
    } as MentionItem))
    rebuildCategories()
  } catch {
    promptCategoryItems.value = []
  }
}

onMounted(() => {
  setPlaceholder()

  if (props.modelValue && inputRef.value) {
    inputRef.value.innerText = props.modelValue
    textContent.value = props.modelValue
  }

  fetchAvailableMentions()
  fetchPromptMentions()
  fetchInstructionMentions()
})

// Rebuild labels when the locale changes so category headings stay localized.
watch(i18nLocale, () => rebuildCategories())

// Reset the highlight when switching between the category-list and item views
// (e.g. the moment the user starts/stops typing a query) so the selection never
// points past the visible rows.
watch(showCategoryList, () => { selectedIndex.value = 0 })

watch(() => props.selectedDataSourceIds, () => {
  fetchAvailableMentions()
}, { deep: true })

watch(() => props.modelValue, (newVal) => {
  if (inputRef.value && newVal !== inputRef.value.innerText) {
    inputRef.value.innerText = newVal
    textContent.value = newVal
  }
})

function scrollSelectedIntoView() {
  if (!dropdownRef.value) return
  const container = dropdownRef.value
  const selectedEl = container.querySelector(`[data-idx="${selectedIndex.value}"]`) as HTMLElement | null
  if (!selectedEl) return
  const cTop = container.scrollTop
  const cBottom = cTop + container.clientHeight
  const eTop = selectedEl.offsetTop
  const eBottom = eTop + selectedEl.offsetHeight
  if (eTop < cTop) {
    container.scrollTop = eTop
  } else if (eBottom > cBottom) {
    container.scrollTop = eBottom - container.clientHeight
  }
}
</script>

<style>
[contenteditable] {
  overflow-y: auto;
  /* `text-align: start` lets dir="auto" choose left vs right from the
   * first strong-direction character — Latin stays LTR, Hebrew goes RTL. */
  text-align: start;
  vertical-align: top;
  line-height: 1.5;
  white-space: pre-wrap;
}

[contenteditable]:empty:before {
  content: attr(data-placeholder);
  color: #9ca3af;
  pointer-events: none;
  font-style: normal;
}

[contenteditable]:focus {
  outline: none;
}

/* Style mentions - Cursor-style minimal design */
.mention {
  display: inline !important;
  padding: 1px 3px;
  border-radius: 3px;
  background-color: rgba(99, 102, 241, 0.10) !important;
  user-select: all;
  white-space: nowrap;
  cursor: pointer;
  transition: background-color 0.15s ease;
}

.mention:hover {
  background-color: rgba(99, 102, 241, 0.15) !important;
}
</style>
