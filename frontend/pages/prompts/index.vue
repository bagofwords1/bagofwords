<template>
  <div class="py-6">
    <div class="max-w-6xl mx-auto px-4">
      <!-- Header -->
      <div class="mb-5">
        <div class="flex items-center justify-between gap-3">
          <h1 class="text-lg font-semibold text-gray-900 dark:text-white">{{ $t('prompts.title') }}</h1>
          <button
            @click="openNew"
            class="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-blue-500 hover:bg-blue-600 rounded-md transition-colors"
          >
            <UIcon name="heroicons-plus" class="w-3.5 h-3.5" />
            {{ $t('prompts.newPrompt') }}
          </button>
        </div>

        <!-- Filters -->
        <div class="mt-3 flex flex-wrap items-center gap-2">
          <div class="relative flex-1 min-w-[180px]">
            <UIcon name="heroicons-magnifying-glass" class="absolute start-2.5 top-2.5 w-4 h-4 text-gray-400" />
            <input
              v-model="search"
              type="text"
              :placeholder="$t('prompts.searchPlaceholder')"
              class="w-full text-sm border border-gray-200 dark:border-gray-700 rounded-md ps-8 pe-3 py-2 dark:bg-gray-800 dark:text-gray-100 dark:placeholder-gray-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
          </div>

          <!-- Scope segmented control -->
          <div class="flex gap-0.5 p-0.5 bg-gray-100 dark:bg-gray-800 rounded">
            <button
              v-for="opt in scopeFilterOptions"
              :key="opt.value"
              class="px-2.5 py-1 text-[11px] rounded transition-colors"
              :class="scopeFilter === opt.value ? 'bg-white dark:bg-gray-900 text-gray-900 dark:text-white shadow-sm font-medium' : 'text-gray-500 hover:text-gray-700'"
              @click="scopeFilter = opt.value"
            >{{ opt.label }}</button>
          </div>

          <!-- Agent filter -->
          <select
            v-model="agentFilter"
            class="text-xs border border-gray-200 dark:border-gray-700 rounded-md px-2 py-2 dark:bg-gray-800 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-500"
          >
            <option :value="''">{{ $t('prompts.allAgents') }}</option>
            <option v-for="a in agents" :key="a.id" :value="a.id">{{ a.name }}</option>
          </select>

          <!-- User filter -->
          <select
            v-model="userFilter"
            class="text-xs border border-gray-200 dark:border-gray-700 rounded-md px-2 py-2 dark:bg-gray-800 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-500"
          >
            <option :value="''">{{ $t('prompts.allUsers') }}</option>
            <option v-for="u in users" :key="u.id" :value="u.id">{{ u.name || u.email }}</option>
          </select>
        </div>
      </div>

      <!-- Loading -->
      <div v-if="isLoading" class="text-xs text-gray-500 dark:text-gray-400 inline-flex items-center">
        <Spinner class="me-1" /> {{ $t('prompts.loading') }}
      </div>

      <!-- Empty -->
      <div v-else-if="prompts.length === 0" class="flex flex-col items-center justify-center text-center py-20 px-4">
        <UIcon name="heroicons-sparkles" class="w-10 h-10 text-gray-300 dark:text-gray-600" />
        <h3 class="mt-3 text-sm font-medium text-gray-900 dark:text-white">
          {{ hasAnyFilter ? $t('prompts.noMatches') : $t('prompts.emptyTitle') }}
        </h3>
        <p class="mt-1 max-w-xs text-xs text-gray-500 dark:text-gray-400">
          {{ hasAnyFilter ? $t('prompts.noMatchesHint') : $t('prompts.emptyHint') }}
        </p>
        <button
          v-if="!hasAnyFilter"
          @click="openNew"
          class="mt-5 inline-flex items-center gap-1.5 h-8 px-3 rounded-md bg-blue-600 text-white text-xs font-medium hover:bg-blue-700"
        >
          <UIcon name="heroicons-plus" class="w-3.5 h-3.5" />
          {{ $t('prompts.createFirst') }}
        </button>
      </div>

      <!-- Card grid -->
      <div v-else class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        <PromptCard
          v-for="p in prompts"
          :key="p.id"
          :prompt="p"
          :agent-names="agentNames"
          :running="runningId === p.id"
          @view="openView"
          @edit="openEdit"
          @delete="confirmDelete"
          @run="onRun"
        />
      </div>
    </div>

    <!-- View modal -->
    <PromptViewModal
      v-model="viewOpen"
      :prompt="activePrompt"
      :agent-names="agentNames"
      :author-names="userNames"
      :running="runningId === activePrompt?.id"
      @run="onRun"
      @edit="openEdit"
      @delete="confirmDelete"
      @run-for="openRunFor"
    />

    <!-- Edit / create modal -->
    <PromptEditModal
      v-model="editOpen"
      :prompt="editPrompt"
      :agents="agents"
      @saved="onSaved"
    />

    <!-- Run-for modal -->
    <RunForModal
      v-model="runForOpen"
      :prompt="activePrompt"
    />

    <!-- Param modal for self-run -->
    <PromptParametersModal
      v-model="paramsOpen"
      :prompt="runParamPrompt"
      @confirm="onParamsConfirm"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import Spinner from '~/components/Spinner.vue'
import PromptCard from '@/components/prompt/PromptCard.vue'
import PromptViewModal from '@/components/prompt/PromptViewModal.vue'
import PromptEditModal from '@/components/prompt/PromptEditModal.vue'
import RunForModal from '@/components/prompt/RunForModal.vue'
import PromptParametersModal from '@/components/prompt/PromptParametersModal.vue'
import { usePrompts } from '~/composables/usePrompts'
import type { Prompt } from '~/composables/usePrompts'
import type { PromptParamValue } from '~/composables/usePromptFill'

definePageMeta({ auth: true })

const { t } = useI18n()
const toast = useToast()
const router = useRouter()
const { organization } = useOrganization()
const { listPrompts, runPrompt, deletePrompt } = usePrompts()

// ── Filters ──
const search = ref('')
const scopeFilter = ref<'' | 'global' | 'agent' | 'private'>('')
const agentFilter = ref('')
const userFilter = ref('')

const scopeFilterOptions = computed(() => [
  { value: '' as const, label: t('prompts.filterAll') },
  { value: 'global' as const, label: t('prompts.scopeGlobal') },
  { value: 'agent' as const, label: t('prompts.scopeAgent') },
  { value: 'private' as const, label: t('prompts.scopePrivate') },
])

const hasAnyFilter = computed(() =>
  !!search.value || !!scopeFilter.value || !!agentFilter.value || !!userFilter.value,
)

// ── Data ──
const prompts = ref<Prompt[]>([])
const isLoading = ref(false)
const agents = ref<{ id: string; name: string }[]>([])
const users = ref<{ id: string; name: string; email: string }[]>([])

const agentNames = computed<Record<string, string>>(() =>
  Object.fromEntries(agents.value.map(a => [a.id, a.name])),
)
const userNames = computed<Record<string, string>>(() =>
  Object.fromEntries(users.value.map(u => [u.id, u.name || u.email])),
)

let loadSeq = 0
async function load() {
  const seq = ++loadSeq
  isLoading.value = true
  try {
    const res = await listPrompts({
      search: search.value || undefined,
      scope: scopeFilter.value || undefined,
      data_source_id: agentFilter.value || undefined,
      created_by: userFilter.value || undefined,
    })
    if (seq === loadSeq) prompts.value = res.prompts || []
  } finally {
    if (seq === loadSeq) isLoading.value = false
  }
}

async function loadAgents() {
  try {
    const { data } = await useMyFetch<any[]>('/data_sources/active', { method: 'GET', query: { include_unconnected: true } })
    agents.value = (data.value || []).map((d: any) => ({ id: d.id, name: d.name }))
  } catch {}
}

async function loadUsers() {
  const orgId = organization.value?.id
  if (!orgId) return
  try {
    const { data } = await useMyFetch<any[]>(`/organizations/${orgId}/members`)
    users.value = (data.value || [])
      .map((m: any) => {
        const id = m.user_id || m.user?.id
        return id ? { id, name: m.user?.name || '', email: m.user?.email || m.email || '' } : null
      })
      .filter(Boolean) as any[]
  } catch {}
}

// Debounced reload on filter changes.
let debounceTimer: any
watch([search, scopeFilter, agentFilter, userFilter], () => {
  clearTimeout(debounceTimer)
  debounceTimer = setTimeout(load, 250)
})

onMounted(() => {
  load()
  loadAgents()
  loadUsers()
})

// ── Modals state ──
const viewOpen = ref(false)
const editOpen = ref(false)
const runForOpen = ref(false)
const paramsOpen = ref(false)
const activePrompt = ref<Prompt | null>(null)
const editPrompt = ref<Prompt | null>(null)
const runParamPrompt = ref<Prompt | null>(null)
const runningId = ref<string | null>(null)

function openView(p: Prompt) {
  activePrompt.value = p
  viewOpen.value = true
}
function openNew() {
  editPrompt.value = null
  editOpen.value = true
}
function openEdit(p: Prompt | null) {
  if (!p) return
  editPrompt.value = p
  viewOpen.value = false
  editOpen.value = true
}
function openRunFor(p: Prompt | null) {
  if (!p) return
  activePrompt.value = p
  viewOpen.value = false
  runForOpen.value = true
}

function onSaved() {
  load()
}

async function confirmDelete(p: Prompt | null) {
  if (!p) return
  if (!confirm(t('prompts.deleteConfirm', { title: p.title || t('prompts.untitled') }))) return
  const ok = await deletePrompt(p.id)
  if (ok) {
    toast.add({ title: t('prompts.toastDeleted'), color: 'green' })
    viewOpen.value = false
    load()
  } else {
    toast.add({ title: t('prompts.toastDeleteFailed'), color: 'red' })
  }
}

// ── Run flow ──
async function onRun(p: Prompt | null) {
  if (!p) return
  if ((p.parameters || []).length > 0) {
    runParamPrompt.value = p
    paramsOpen.value = true
    return
  }
  await doRun(p, undefined)
}

async function onParamsConfirm(values: Record<string, PromptParamValue>) {
  const p = runParamPrompt.value
  if (!p) return
  await doRun(p, values)
}

async function doRun(p: Prompt, values?: Record<string, PromptParamValue>) {
  runningId.value = p.id
  try {
    const res = await runPrompt(p.id, values)
    if (res?.report_id) {
      viewOpen.value = false
      router.push(`/reports/${res.report_id}`)
    } else {
      toast.add({ title: t('prompts.toastRunFailed'), color: 'red' })
    }
  } catch (e: any) {
    toast.add({ title: e?.data?.detail || t('prompts.toastRunFailed'), color: 'red' })
  } finally {
    runningId.value = null
  }
}
</script>
