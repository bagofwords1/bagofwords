<template>
  <div class="py-6">
    <div class="max-w-5xl mx-auto px-4">
      <!-- Header -->
      <div class="mb-5">
        <div class="flex items-center justify-between">
          <h1 class="text-lg font-semibold text-gray-900">Prompt Catalog</h1>
          <UButton v-if="canCreate" size="xs" color="blue" icon="i-heroicons-plus" @click="openCreate">New prompt</UButton>
        </div>
        <p class="mt-1 text-xs text-gray-500">Browse reusable prompts, run them now, or subscribe to scheduled runs.</p>
      </div>

      <!-- Tabs: Catalog / My Subscriptions -->
      <div class="flex gap-0.5 p-0.5 bg-gray-100 rounded w-fit mb-4">
        <button
          v-for="tab in tabs"
          :key="tab.value"
          class="px-3 py-1 text-xs rounded transition-colors"
          :class="activeTab === tab.value ? 'bg-white text-gray-900 shadow-sm font-medium' : 'text-gray-400 hover:text-gray-600'"
          @click="activeTab = tab.value"
        >
          {{ tab.label }}
        </button>
      </div>

      <!-- ===== Catalog tab ===== -->
      <template v-if="activeTab === 'catalog'">
        <!-- Controls -->
        <div class="flex items-center justify-between gap-3 mb-4 flex-wrap">
          <div class="flex gap-0.5 p-0.5 bg-gray-100 rounded w-fit">
            <button
              v-for="s in sorts"
              :key="s.value"
              class="px-3 py-1 text-xs rounded transition-colors"
              :class="sort === s.value ? 'bg-white text-gray-900 shadow-sm font-medium' : 'text-gray-400 hover:text-gray-600'"
              @click="setSort(s.value)"
            >
              {{ s.label }}
            </button>
          </div>

          <div class="flex items-center gap-2">
            <select v-model="category" class="rounded border border-gray-200 px-2 py-1.5 text-xs" @change="reload">
              <option value="">All categories</option>
              <option v-for="c in categories" :key="c" :value="c">{{ c }}</option>
            </select>
            <label class="flex items-center gap-1.5 text-xs text-gray-600 cursor-pointer">
              <UCheckbox v-model="startersOnly" @change="reload" />
              Starters only
            </label>
          </div>
        </div>

        <!-- Loading -->
        <div v-if="isLoading" class="text-xs text-gray-500 inline-flex items-center">
          <Spinner class="me-1" /> Loading prompts…
        </div>

        <!-- Empty -->
        <div v-else-if="prompts.length === 0" class="py-16 text-center text-xs text-gray-500">
          No prompts found.
        </div>

        <!-- Top section (only when sort=top) -->
        <template v-else>
          <div v-if="sort === 'top' && topPrompts.length" class="mb-6">
            <div class="text-xs font-medium text-gray-500 uppercase tracking-wider mb-2">Top prompts</div>
            <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              <PromptCard
                v-for="p in topPrompts"
                :key="p.id"
                :prompt="p"
                :running="runningId === p.id"
                @open="openDetail"
                @run="runPromptAction"
                @subscribe="openSubscribe"
                @assign="openAssign"
                @edit="openEdit"
              />
            </div>
          </div>

          <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            <PromptCard
              v-for="p in restPrompts"
              :key="p.id"
              :prompt="p"
              :running="runningId === p.id"
              @open="openDetail"
              @run="runPromptAction"
              @subscribe="openSubscribe"
              @assign="openAssign"
              @edit="openEdit"
            />
          </div>

          <div class="mt-6 text-center text-[11px] text-gray-500">
            Showing {{ prompts.length }} of {{ total }} prompts
          </div>
        </template>
      </template>

      <!-- ===== My Subscriptions tab ===== -->
      <template v-else>
        <div v-if="subsLoading" class="text-xs text-gray-500 inline-flex items-center">
          <Spinner class="me-1" /> Loading subscriptions…
        </div>
        <div v-else-if="subscriptions.length === 0" class="py-16 text-center text-xs text-gray-500">
          You have no scheduled prompts yet.
        </div>
        <div v-else class="space-y-3">
          <div
            v-for="sub in subscriptions"
            :key="sub.id"
            class="border border-gray-100 bg-white rounded-lg p-4 flex items-start justify-between gap-3"
          >
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2 mb-1">
                <span
                  class="text-[10px] px-1.5 py-0.5 rounded border"
                  :class="sub.is_active ? 'text-green-700 border-green-200 bg-green-50' : 'text-gray-700 border-gray-200 bg-gray-50'"
                >{{ sub.is_active ? 'Active' : 'Paused' }}</span>
                <span class="text-[11px] text-gray-400">{{ getCronLabel(sub.cron_schedule) }}</span>
              </div>
              <div class="text-sm font-medium text-gray-900 line-clamp-2">{{ sub.prompt?.content || sub.prompt?.title || 'Untitled' }}</div>
            </div>
            <div class="flex items-center gap-1.5 flex-shrink-0">
              <UButton size="2xs" color="gray" variant="soft" @click="togglePause(sub)">
                {{ sub.is_active ? 'Pause' : 'Resume' }}
              </UButton>
              <UButton size="2xs" color="red" variant="ghost" icon="i-heroicons-trash" @click="deleteSub(sub)" />
            </div>
          </div>
        </div>
      </template>
    </div>

    <!-- Detail drawer -->
    <PromptDetailDrawer
      v-model="showDetail"
      :prompt="selectedPrompt"
      :running="runningId === selectedPrompt?.id"
      :deleting="deletingId === selectedPrompt?.id"
      :data-sources="dataSources"
      @run="runPromptAction"
      @subscribe="openSubscribe"
      @assign="openAssign"
      @edit="openEdit"
      @delete="deletePromptAction"
    />

    <!-- Subscribe modal -->
    <PromptSubscribeModal v-model="showSubscribe" :prompt="selectedPrompt" @subscribed="onSubscribed" />

    <!-- Assign modal -->
    <PromptAssignModal v-model="showAssign" :prompt="selectedPrompt" @assigned="reload" />

    <!-- Author / edit modal -->
    <PromptEditModal v-model="showEdit" :prompt="editingPrompt" @saved="reload" />
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import PromptCard from '~/components/prompt/PromptCard.vue'
import PromptDetailDrawer from '~/components/prompt/PromptDetailDrawer.vue'
import PromptSubscribeModal from '~/components/prompt/PromptSubscribeModal.vue'
import PromptAssignModal from '~/components/prompt/PromptAssignModal.vue'
import PromptEditModal from '~/components/prompt/PromptEditModal.vue'
import type { PromptResponse, PromptSort } from '~/composables/usePrompts'

definePageMeta({ auth: true })

const router = useRouter()
const toast = useToast()
const { getCronLabel } = useCronLabel()
const { listPrompts, getPrompt, runPrompt, deletePrompt } = usePrompts()

// "manage_settings" covers full admins; full_admin_access is handled by useCan.
// Authoring visibility is also gated per-prompt via can_manage on the cards.
const canCreate = computed(() => useCan('manage_settings') || useCan('create_reports'))

const tabs = [
  { value: 'catalog', label: 'Catalog' },
  { value: 'subscriptions', label: 'My Subscriptions' },
] as const
const activeTab = ref<'catalog' | 'subscriptions'>('catalog')

const sorts: { value: PromptSort; label: string }[] = [
  { value: 'top', label: 'Top' },
  { value: 'recent', label: 'Recent' },
]
const sort = ref<PromptSort>('top')
const category = ref('')
const startersOnly = ref(false)

const prompts = ref<PromptResponse[]>([])
const total = ref(0)
const isLoading = ref(true)
const runningId = ref<string | null>(null)
const deletingId = ref<string | null>(null)

// Distinct categories present in the loaded set (used to populate the filter).
const categories = computed(() => {
  const set = new Set<string>()
  for (const p of prompts.value) if (p.category) set.add(p.category)
  return Array.from(set).sort()
})

// When sorted by "top", surface the leading few as a highlighted section.
const TOP_COUNT = 3
const topPrompts = computed(() => (sort.value === 'top' ? prompts.value.slice(0, TOP_COUNT) : []))
const restPrompts = computed(() => (sort.value === 'top' ? prompts.value.slice(TOP_COUNT) : prompts.value))

// Optional data-source name/type lookup for the detail drawer.
const dataSources = ref<{ id: string; name: string; type?: string }[]>([])

async function reload() {
  isLoading.value = true
  try {
    const data = await listPrompts({
      sort: sort.value,
      category: category.value || undefined,
      starters_only: startersOnly.value || undefined,
    })
    prompts.value = data.prompts || []
    total.value = data.meta?.total ?? prompts.value.length
  } catch (e: any) {
    toast.add({ title: 'Failed to load prompts', description: e?.data?.detail || e?.message, color: 'red' })
    prompts.value = []
  } finally {
    isLoading.value = false
  }
}

function setSort(value: PromptSort) {
  if (sort.value === value) return
  sort.value = value
  reload()
}

async function loadDataSources() {
  try {
    const res = await useMyFetch('/data_sources/active', { query: { include_unconnected: true } })
    if (res.data.value && Array.isArray(res.data.value)) {
      dataSources.value = (res.data.value as any[]).map((d) => ({ id: d.id, name: d.name, type: d.type }))
    }
  } catch {}
}

// ---- Selection / modal state ----
const selectedPrompt = ref<PromptResponse | null>(null)
const editingPrompt = ref<PromptResponse | null>(null)
const showDetail = ref(false)
const showSubscribe = ref(false)
const showAssign = ref(false)
const showEdit = ref(false)

async function openDetail(p: PromptResponse) {
  selectedPrompt.value = p
  showDetail.value = true
  // Refresh with full detail (agents/mentions) in the background.
  try {
    selectedPrompt.value = await getPrompt(p.id)
  } catch {}
}

function openSubscribe(p: PromptResponse) {
  selectedPrompt.value = p
  showSubscribe.value = true
}

function openAssign(p: PromptResponse) {
  selectedPrompt.value = p
  showAssign.value = true
}

function openEdit(p: PromptResponse) {
  editingPrompt.value = p
  showEdit.value = true
}

function openCreate() {
  editingPrompt.value = null
  showEdit.value = true
}

async function runPromptAction(p: PromptResponse) {
  if (runningId.value) return
  runningId.value = p.id
  try {
    const { report_id } = await runPrompt(p.id)
    toast.add({ title: 'Running prompt…', color: 'green' })
    router.push(`/reports/${report_id}`)
  } catch (e: any) {
    toast.add({ title: 'Failed to run prompt', description: e?.data?.detail || e?.message, color: 'red' })
  } finally {
    runningId.value = null
  }
}

async function deletePromptAction(p: PromptResponse) {
  if (!confirm(`Delete prompt "${p.title}"?`)) return
  deletingId.value = p.id
  try {
    await deletePrompt(p.id)
    toast.add({ title: 'Prompt deleted', color: 'green' })
    showDetail.value = false
    reload()
  } catch (e: any) {
    toast.add({ title: 'Failed to delete prompt', description: e?.data?.detail || e?.message, color: 'red' })
  } finally {
    deletingId.value = null
  }
}

function onSubscribed() {
  reload()
  if (activeTab.value === 'subscriptions') fetchSubscriptions()
}

// ---- My Subscriptions (reuses existing scheduled-prompts API) ----
const subscriptions = ref<any[]>([])
const subsLoading = ref(false)

async function fetchSubscriptions() {
  subsLoading.value = true
  try {
    const res = await useMyFetch('/scheduled-prompts', {
      method: 'GET',
      query: { page: 1, limit: 50, filter: 'my' },
    })
    if (res.status.value === 'success' && res.data.value) {
      subscriptions.value = (res.data.value as any).scheduled_prompts || []
    } else {
      subscriptions.value = []
    }
  } catch {
    subscriptions.value = []
  } finally {
    subsLoading.value = false
  }
}

async function togglePause(sub: any) {
  const next = !sub.is_active
  try {
    const res = await useMyFetch(`/reports/${sub.report_id}/scheduled-prompts/${sub.id}`, {
      method: 'PUT',
      body: { is_active: next },
    })
    if (res.status.value === 'success') {
      sub.is_active = next
    } else {
      throw new Error('update failed')
    }
  } catch (e: any) {
    toast.add({ title: 'Failed to update', description: e?.data?.detail || e?.message, color: 'red' })
  }
}

async function deleteSub(sub: any) {
  if (!confirm('Delete this scheduled prompt?')) return
  try {
    const res = await useMyFetch(`/reports/${sub.report_id}/scheduled-prompts/${sub.id}`, { method: 'DELETE' })
    if (res.status.value === 'success') {
      subscriptions.value = subscriptions.value.filter((s) => s.id !== sub.id)
      toast.add({ title: 'Deleted', color: 'green' })
    } else {
      throw new Error('delete failed')
    }
  } catch (e: any) {
    toast.add({ title: 'Failed to delete', description: e?.data?.detail || e?.message, color: 'red' })
  }
}

watch(activeTab, (tab) => {
  if (tab === 'subscriptions' && subscriptions.value.length === 0) fetchSubscriptions()
})

onMounted(async () => {
  await nextTick()
  reload()
  loadDataSources()
})
</script>

<style scoped>
.line-clamp-1 {
  display: -webkit-box;
  -webkit-line-clamp: 1;
  line-clamp: 1;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
