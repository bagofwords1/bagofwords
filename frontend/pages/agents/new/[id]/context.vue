<template>
  <div class="min-h-screen py-10 px-4 md:w-1/2 mx-auto text-sm">
    <div class="w-full px-4 ps-0 py-4">
      <div>
        <h1 class="text-lg font-semibold text-center">Create Data Agent</h1>
        <p class="mt-4 text-gray-500 text-center">Set data source, select tables, and define additional context</p>
      </div>
      <WizardSteps class="mb-5 mt-4" current="context" :ds-id="dsId" />

      <div class="space-y-6">
        <!-- Instruction editor -->
        <div>
          <h3 class="text-lg font-semibold text-gray-900 mb-1">Add custom AI rules and instructions</h3>
          <p class="text-sm text-gray-500 mb-3">Business-specific context, glossary, and useful code guidelines.</p>

          <!-- outer wrapper is relative so dropdown can overflow the border box -->
          <div class="relative">
            <div class="border border-gray-200 focus-within:ring-2 focus-within:ring-blue-100 focus-within:border-blue-400">
              <!-- Loading overlay -->
              <div v-if="loadingDraft" class="flex items-center justify-center gap-2 py-10 text-xs text-gray-400">
                <Spinner class="w-4 h-4" />
                <span>Generating overview instruction…</span>
              </div>

              <div v-else class="mention-container">
                <!-- Highlight backdrop -->
                <div ref="backdropRef" class="mention-backdrop" aria-hidden="true" v-html="highlightedText" />

                <!-- Textarea -->
                <textarea
                  ref="textareaRef"
                  v-model="instructionText"
                  placeholder="Describe business rules, metric definitions, or query guidelines…"
                  class="mention-textarea"
                  @input="handleTextareaInput"
                  @keydown="handleTextareaKeydown"
                  @blur="handleTextareaBlur"
                  @scroll="syncScroll"
                />
              </div>
            </div>

            <!-- @ Mention dropdown — outside overflow container so it can spill out -->
            <div
              v-if="mentionState.active && (filteredMentionItems.length > 0 || mentionState.query.length < 5)"
              ref="mentionDropdownRef"
              class="absolute z-50 bg-white border border-gray-200 rounded-lg shadow-lg max-h-48 overflow-y-auto w-80"
              :style="mentionDropdownStyle"
            >
              <div v-if="filteredMentionItems.length === 0" class="px-3 py-2 text-xs text-gray-500">
                Type to search…
              </div>
              <button
                v-for="(item, index) in filteredMentionItems"
                :key="item.id"
                type="button"
                :data-mention-idx="index"
                class="w-full text-start px-3 py-2 text-xs hover:bg-gray-50 flex items-start gap-2 border-b border-gray-100 last:border-0"
                :class="{ 'bg-blue-50': index === mentionState.selectedIndex }"
                @mousedown.prevent="selectMention(item)"
              >
                <template v-if="item.type === 'instruction'">
                  <Icon name="heroicons:cube" class="w-3.5 h-3.5 mt-0.5 shrink-0 text-indigo-500" />
                </template>
                <template v-else-if="item.type === 'connection_tool'">
                  <span class="relative inline-flex shrink-0 mt-0.5">
                    <DataSourceIcon v-if="item.dataSourceType" :type="item.dataSourceType" class="h-3.5" />
                    <Icon v-else name="heroicons:table-cells" class="w-3.5 h-3.5 text-blue-500" />
                    <Icon name="heroicons:wrench-screwdriver" class="absolute -bottom-0.5 -right-1 w-2 h-2 text-indigo-400" />
                  </span>
                </template>
                <template v-else>
                  <Icon name="heroicons:table-cells" class="w-3.5 h-3.5 mt-0.5 shrink-0 text-blue-500" />
                </template>
                <div class="flex-1 min-w-0">
                  <template v-if="item.type === 'instruction'">
                    <span v-if="item.name" class="font-mono font-medium text-gray-900 block">{{ item.name }}</span>
                    <span v-else class="text-gray-700 truncate block">"{{ item.textPreview?.slice(0, 30) }}..."</span>
                  </template>
                  <template v-else>
                    <span class="font-mono font-medium text-gray-900 block">{{ item.name }}</span>
                    <div class="flex items-center gap-1 mt-0.5">
                      <DataSourceIcon v-if="item.dataSourceType && item.type !== 'connection_tool'" :type="item.dataSourceType" class="h-2.5" />
                      <span class="text-[10px] text-gray-500">{{ item.dataSourceName }}</span>
                    </div>
                  </template>
                </div>
              </button>
            </div>
          </div>
        </div>

        <!-- Git integration — only shown when no repo is connected -->
        <div v-if="!integration?.git_repository" class="flex items-center gap-1.5 text-xs text-gray-400">
          <GitBranchIcon class="w-3.5 h-3.5" />
          <span>Connect a git repository for Tableau, dbt, and markdown context —</span>
          <button class="text-blue-500 hover:text-blue-600 underline-offset-2 hover:underline" @click="showGitModal = true">integrate</button>
        </div>

        <div class="flex justify-end pt-4">
          <button @click="handleSave" :disabled="saving || loadingDraft" class="bg-blue-500 hover:bg-blue-600 text-white text-xs font-medium py-1.5 px-3 rounded disabled:opacity-50">
            <span v-if="saving">Saving...</span>
            <span v-else>Save & Continue</span>
          </button>
        </div>

        <GitRepoModalComponent v-model="showGitModal" :datasource-id="String(dsId)" :git-repository="integration?.git_repository" :metadata-resources="{ resources: [] }" @update:modelValue="handleGitModalClose" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true })
import WizardSteps from '@/components/datasources/WizardSteps.vue'
import GitRepoModalComponent from '@/components/GitRepoModalComponent.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import GitBranchIcon from '~/components/icons/GitBranchIcon.vue'
import Spinner from '~/components/Spinner.vue'

const route = useRoute()
const router = useRouter()
const dsId = computed(() => String(route.params.id || ''))

const saving = ref(false)
const loadingDraft = ref(false)
const showGitModal = ref(false)
const integration = ref<any>(null)
const draftInstructionId = ref<string | null>(null)

async function fetchIntegration() {
  if (!dsId.value) return
  const response = await useMyFetch(`/data_sources/${dsId.value}`, { method: 'GET' })
  if ((response.status as any)?.value === 'success') integration.value = (response.data as any)?.value
}

function handleGitModalClose(value: boolean) { if (!value) fetchIntegration() }

// ── Draft instruction ─────────────────────────────────────────────────────────
async function loadDraftInstruction() {
  if (!dsId.value) return
  loadingDraft.value = true
  try {
    // Trigger llm_sync — returns the onboarding_instruction id directly
    const { data: syncData } = await useMyFetch<any>(`/data_sources/${dsId.value}/llm_sync`, { method: 'POST' })
    const instructionId = syncData.value?.onboarding_instruction?.id

    if (instructionId) {
      // Fetch the specific instruction by ID
      const { data, error } = await useMyFetch<any>(`/instructions/${instructionId}`, { method: 'GET' })
      if (!error.value && data.value) {
        instructionText.value = data.value.text || ''
        draftInstructionId.value = instructionId
      }
    }
  } catch {} finally {
    loadingDraft.value = false
  }
}

// ── Instruction text ──────────────────────────────────────────────────────────
const instructionText = ref('')

// ── @ Mention ─────────────────────────────────────────────────────────────────
interface MentionItem {
  id: string
  type: 'instruction' | 'metadata_resource' | 'datasource_table' | 'connection_tool'
  name: string | null
  textPreview: string | null
  dataSourceId: string | null
  dataSourceName: string | null
  dataSourceType: string | null
}

const textareaRef = ref<HTMLTextAreaElement | null>(null)
const backdropRef = ref<HTMLDivElement | null>(null)
const mentionDropdownRef = ref<HTMLDivElement | null>(null)

const allMentionItems = ref<MentionItem[]>([])
const mentionSearchResults = ref<MentionItem[]>([])
const isFetchingMentions = ref(false)

const mentionState = ref({
  active: false,
  query: '',
  startPos: 0,
  selectedIndex: 0,
  top: 0,
  left: 0,
})

const filteredMentionItems = computed(() => {
  const instructions = mentionSearchResults.value.filter(i => i.type === 'instruction').slice(0, 3)
  const tables = mentionSearchResults.value.filter(i => i.type === 'datasource_table' || i.type === 'metadata_resource').slice(0, 3)
  const tools = mentionSearchResults.value.filter(i => i.type === 'connection_tool').slice(0, 3)
  return [...instructions, ...tables, ...tools]
})

const mentionDropdownStyle = computed(() => ({
  top: `${mentionState.value.top}px`,
  left: `${mentionState.value.left}px`,
}))

const fetchAllMentionItems = async () => {
  try {
    const params = new URLSearchParams()
    params.set('types', 'instruction,datasource_table,metadata_resource,connection_tool')
    params.set('data_source_filter', dsId.value)
    const { data, error } = await useMyFetch<any[]>(`/instructions/available-references?${params}`, { method: 'GET' })
    if (!error.value && data.value) {
      allMentionItems.value = data.value.map(item => ({
        id: item.id, type: item.type, name: item.name, textPreview: item.text_preview || null,
        dataSourceId: item.data_source_id, dataSourceName: item.data_source_name, dataSourceType: item.data_source_type
      }))
    }
  } catch {}
}

const fetchMentionItems = async (query?: string) => {
  isFetchingMentions.value = true
  try {
    const params = new URLSearchParams()
    if (query) params.set('q', query)
    params.set('types', 'instruction,datasource_table,metadata_resource,connection_tool')
    params.set('data_source_filter', dsId.value)
    const { data, error } = await useMyFetch<any[]>(`/instructions/available-references?${params}`, { method: 'GET' })
    if (!error.value && data.value) {
      mentionSearchResults.value = data.value.map(item => ({
        id: item.id, type: item.type, name: item.name, textPreview: item.text_preview || null,
        dataSourceId: item.data_source_id, dataSourceName: item.data_source_name, dataSourceType: item.data_source_type
      }))
    }
  } catch {} finally {
    isFetchingMentions.value = false
  }
}

let mentionFetchTimeout: ReturnType<typeof setTimeout> | null = null
watch(() => mentionState.value.query, (q) => {
  if (mentionFetchTimeout) clearTimeout(mentionFetchTimeout)
  mentionFetchTimeout = setTimeout(() => fetchMentionItems(q), 150)
})

const isKnownMention = (text: string) => {
  const lower = text.toLowerCase()
  return allMentionItems.value.some(item => {
    if (item.name?.toLowerCase() === lower) return true
    if (item.type === 'instruction' && item.textPreview) {
      if ((item.textPreview.slice(0, 30) + '...').toLowerCase() === lower) return true
    }
    return false
  })
}

const highlightedText = computed(() => {
  const text = instructionText.value
  if (!text) return ''
  let html = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  html = html.replace(/@([A-Za-z_][A-Za-z0-9_]*|"[^"]+")/g, (match, captured) => {
    let name = captured
    if (name.startsWith('"') && name.endsWith('"')) name = name.slice(1, -1)
    if (isKnownMention(name)) {
      return `<mark style="background-color: rgba(99,102,241,0.12); color: transparent; border-radius: 3px; padding: 0;">${match}</mark>`
    }
    return match
  })
  return html + '\n'
})

const syncScroll = (e: Event) => {
  const ta = e.target as HTMLTextAreaElement
  if (backdropRef.value) backdropRef.value.scrollTop = ta.scrollTop
}

const handleTextareaInput = (e: Event) => {
  const ta = e.target as HTMLTextAreaElement
  const text = ta.value
  const cursorPos = ta.selectionStart
  const textBeforeCursor = text.slice(0, cursorPos)
  const atIndex = textBeforeCursor.lastIndexOf('@')

  if (atIndex !== -1) {
    const textAfterAt = textBeforeCursor.slice(atIndex + 1)
    if (!textAfterAt.includes('\n') && !textAfterAt.includes('  ') && textAfterAt.length <= 50) {
      const lines = textBeforeCursor.split('\n')
      const lineIndex = lines.length - 1
      mentionState.value = {
        active: true, query: textAfterAt, startPos: atIndex, selectedIndex: 0,
        top: (lineIndex + 1) * 18 + 16,
        left: Math.min((lines[lineIndex].length - textAfterAt.length) * 7 + 16, 200),
      }
      return
    }
  }
  if (mentionState.value.active) mentionState.value.active = false
}

const scrollMentionIntoView = () => {
  if (!mentionDropdownRef.value) return
  const container = mentionDropdownRef.value
  const el = container.querySelector(`[data-mention-idx="${mentionState.value.selectedIndex}"]`) as HTMLElement | null
  if (!el) return
  const top = container.scrollTop, bottom = top + container.clientHeight
  if (el.offsetTop < top) container.scrollTop = el.offsetTop
  else if (el.offsetTop + el.offsetHeight > bottom) container.scrollTop = el.offsetTop + el.offsetHeight - container.clientHeight
}

const handleTextareaKeydown = (e: KeyboardEvent) => {
  if (!mentionState.value.active) return
  const items = filteredMentionItems.value
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    mentionState.value.selectedIndex = Math.min(mentionState.value.selectedIndex + 1, items.length - 1)
    nextTick(scrollMentionIntoView)
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    mentionState.value.selectedIndex = Math.max(mentionState.value.selectedIndex - 1, 0)
    nextTick(scrollMentionIntoView)
  } else if (e.key === 'Enter' || e.key === 'Tab') {
    if (items.length > 0) { e.preventDefault(); selectMention(items[mentionState.value.selectedIndex]) }
  } else if (e.key === 'Escape') {
    e.preventDefault(); mentionState.value.active = false
  }
}

const handleTextareaBlur = () => setTimeout(() => { mentionState.value.active = false }, 150)

const needsQuotes = (name: string | null) => name && /[\s\-.]/.test(name)

const selectMention = (item: MentionItem) => {
  const ta = textareaRef.value
  if (!ta) return
  const { startPos, query } = mentionState.value
  let mentionText: string
  if (!item.name) {
    mentionText = `@"${item.textPreview?.slice(0, 30)}..."`
  } else if (needsQuotes(item.name)) {
    mentionText = `@"${item.name}"`
  } else {
    mentionText = `@${item.name}`
  }
  const before = instructionText.value.slice(0, startPos)
  const after = instructionText.value.slice(startPos + 1 + query.length)
  instructionText.value = before + mentionText + ' ' + after
  mentionState.value.active = false
  nextTick(() => {
    ta.focus()
    const pos = startPos + mentionText.length + 1
    ta.setSelectionRange(pos, pos)
  })
}

// ── Save ──────────────────────────────────────────────────────────────────────
async function handleSave() {
  if (saving.value) return
  saving.value = true
  try {
    const text = instructionText.value.trim()

    if (draftInstructionId.value) {
      if (text) {
        // Promote existing draft to published with (possibly edited) text
        await useMyFetch(`/instructions/${draftInstructionId.value}`, {
          method: 'PUT',
          body: { text, status: 'published' },
        })
      } else {
        // User cleared the text — discard the draft
        await useMyFetch(`/instructions/${draftInstructionId.value}`, { method: 'DELETE' })
      }
    } else if (text) {
      // No draft exists (llm_sync hadn't run yet) — create fresh
      await useMyFetch('/instructions/global', {
        method: 'POST',
        body: {
          text,
          status: 'published',
          category: 'general',
          is_seen: true,
          can_user_toggle: true,
          load_mode: 'always',
          data_source_ids: [dsId.value],
        },
      })
    }

    router.replace(`/agents/${dsId.value}`)
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  fetchIntegration()
  loadDraftInstruction()
  fetchAllMentionItems()
  fetchMentionItems()
})
</script>

<style scoped>
.mention-container {
  position: relative;
}

.mention-backdrop,
.mention-textarea {
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
  font-size: 12px;
  line-height: 1.625;
  padding: 16px;
  margin: 0;
  border: 0;
  white-space: pre-wrap;
  word-wrap: break-word;
  overflow-wrap: break-word;
  letter-spacing: normal;
  word-spacing: normal;
}

.mention-backdrop {
  position: absolute;
  inset: 0;
  color: transparent;
  pointer-events: none;
  overflow: hidden;
  background: transparent;
}

.mention-textarea {
  position: relative;
  z-index: 1;
  width: 100%;
  min-height: 360px;
  resize: vertical;
  background: transparent;
  caret-color: #111827;
  outline: none;
}

.mention-textarea::placeholder {
  color: #9ca3af;
}

.mention-backdrop :deep(mark) {
  color: transparent;
  border-radius: 3px;
  padding: 2px 0;
  box-decoration-break: clone;
}
</style>
