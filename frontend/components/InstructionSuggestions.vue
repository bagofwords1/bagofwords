<template>
  <!-- Hide entirely when not loading and no drafts -->
  <div v-if="isLoading || drafts.length > 0" class="mb-2">
    <!-- Title section -->
    <div class="flex items-center text-xs text-gray-500 mb-3">
      <!-- Status icon -->
      <Spinner v-if="isLoading" class="w-3 h-3 mr-1.5 text-gray-400" />
      <Icon v-else name="heroicons-light-bulb" class="w-3 h-3 mr-1.5 text-green-500" />

      <!-- Title with shimmer for loading -->
      <span v-if="isLoading" class="tool-shimmer">
        Suggesting Instructions...
      </span>
      <span v-else class="text-gray-700">
        Suggested {{ drafts.length }} instruction{{ drafts.length === 1 ? '' : 's' }}
      </span>
    </div>

    <!-- Instruction cards -->
    <div class="space-y-2" v-if="drafts.length">
      <div
        v-for="(d, i) in drafts"
        :key="d.id || i"
        :class="[
          'border border-gray-150 rounded-md p-3 transition-colors',
          !isBuildPublished && !selectedIds.has(d.id) ? 'opacity-50 bg-gray-50' : 'hover:bg-gray-50'
        ]"
      >
        <div class="flex items-start gap-2">
          <!-- Checkbox for selection (only when not published) -->
          <UCheckbox
            v-if="!isBuildPublished && d.id"
            :model-value="selectedIds.has(d.id)"
            color="blue"
            @update:model-value="toggleSelection(d.id, $event)"
            class="mt-0.5"
          />
          <div class="flex-1">
            <div
              @click="!isBuildPublished ? handleEdit(d, i) : null"
              :class="[
                'text-[12px] text-gray-800 leading-relaxed',
                isBuildPublished ? '' : 'cursor-pointer'
              ]"
            >
              {{ d.text }}
            </div>
            <div v-if="d.category" class="text-xs hidden text-gray-500 mt-1 font-medium">{{ d.category }}</div>
          </div>
        </div>

        <!-- Action buttons for unpublished builds -->
        <div v-if="!isBuildPublished" class="flex justify-start gap-2 pt-2 mt-2 border-t border-gray-200">
          <button
            @click="handleEdit(d, i)"
            class="flex items-center text-[9px] text-gray-500 hover:text-gray-700 transition-colors"
          >
            <Icon name="heroicons:pencil" class="w-2.5 h-2.5 mr-0.5" />
            <span>Edit</span>
          </button>
        </div>
      </div>
    </div>

    <!-- Publish Instructions button -->
    <div v-if="drafts.length > 0 && !isLoading && !isBuildPublished && canCreateInstructions" class="mt-2">
      <UButton
        variant="soft"
        color="blue"
        size="xs"
        :disabled="isPublishingBuild || selectedIds.size === 0"
        @click="handlePublishBuild"
      >
        <template #leading>
          <Spinner v-if="isPublishingBuild" class="w-3 h-3" />
          <Icon v-else name="heroicons:arrow-up-tray" class="w-3 h-3" />
        </template>
        {{ publishButtonText }}
      </UButton>
    </div>

    <!-- Published status (shown in place of button after publishing) -->
    <div v-if="drafts.length > 0 && !isLoading && isBuildPublished" class="mt-2 flex items-center text-xs text-green-600">
      <Icon name="heroicons:check-circle-solid" class="w-4 h-4 mr-1.5" />
      <span class="font-medium">Published</span>
      <span v-if="publishedAtFormatted" class="text-gray-500 ml-1">
        at {{ publishedAtFormatted }}
      </span>
    </div>

    <!-- Instruction Modal -->
    <InstructionModalComponent
      v-model="showInstructionModal"
      :instruction="editingInstruction"
      :initial-type="modalInitialType"
      :is-suggestion="true"
      :target-build-id="buildId"
      @instruction-saved="handleInstructionSaved"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import InstructionModalComponent from '~/components/InstructionModalComponent.vue'
import Spinner from '~/components/Spinner.vue'

interface ToolExecution {
  id: string
  tool_name: string
  status: string
  result_json?: any
}

interface InstructionDraft {
  id: string
  text: string
  category: string
  status: string
  private_status?: string | null
  global_status?: string | null
  is_seen: boolean
  can_user_toggle: boolean
  user_id?: string | null
  organization_id: string
  agent_execution_id?: string | null
  trigger_reason?: string | null
  created_at?: string | null
  updated_at?: string | null
  build_id?: string | null
  build_status?: string | null
  build_is_main?: boolean
  build_approved_at?: string | null
}

interface Props {
  toolExecution: ToolExecution
}

const props = defineProps<Props>()

// Reactive state
const showInstructionModal = ref(false)
const editingInstruction = ref<any>(null)
const modalInitialType = ref<'global' | 'private'>('private')
const isPublishingBuild = ref(false)
const isRemoving = ref(false)
const removingIndex = ref(-1)
const localPublishOverride = ref(false) // Used to show published state immediately after publishing
const selectedIds = ref<Set<string>>(new Set())

// Composables
const toast = useToast()

const drafts = computed<InstructionDraft[]>(() => {
  const rj = props.toolExecution?.result_json || {}
  const out = rj?.drafts || rj?.instructions
  if (Array.isArray(out)) {
    return out.map((d: any) => ({
      id: d?.id || '',
      text: String(d?.text || ''),
      category: d?.category || 'general',
      status: d?.status || 'draft',
      private_status: d?.private_status || null,
      global_status: d?.global_status || null,
      is_seen: d?.is_seen ?? true,
      can_user_toggle: d?.can_user_toggle ?? true,
      user_id: d?.user_id || null,
      organization_id: d?.organization_id || '',
      agent_execution_id: d?.agent_execution_id || null,
      trigger_reason: d?.trigger_reason || null,
      created_at: d?.created_at || null,
      updated_at: d?.updated_at || null,
      build_id: d?.build_id || null,
      build_status: d?.build_status || null,
      build_is_main: d?.build_is_main ?? false,
      build_approved_at: d?.build_approved_at || null,
    })).filter(d => d.text)
  }
  return []
})

// Get build_id from first draft (all drafts in a suggestion share the same build)
const buildId = computed(() => {
  return drafts.value[0]?.build_id || null
})

const isLoading = computed(() => {
  return props.toolExecution?.status === 'running' || props.toolExecution?.status === 'in_progress'
})

const canCreateInstructions = computed(() => {
  return useCan('create_instructions')
})

// Check if build is published - from data (persists on refresh) or local override (immediate feedback)
const isBuildPublished = computed(() => {
  // Local override for immediate UI feedback after publishing
  if (localPublishOverride.value) return true
  // Check build status from backend data
  const firstDraft = drafts.value[0]
  return firstDraft?.build_is_main === true || firstDraft?.build_status === 'approved'
})

// Format the published timestamp
const publishedAtFormatted = computed(() => {
  const firstDraft = drafts.value[0]
  if (!firstDraft?.build_approved_at) return null
  try {
    const date = new Date(firstDraft.build_approved_at)
    return date.toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    })
  } catch {
    return null
  }
})

// Toggle selection of an instruction
const toggleSelection = (id: string, checked: boolean) => {
  const newSet = new Set(selectedIds.value)
  if (checked) {
    newSet.add(id)
  } else {
    newSet.delete(id)
  }
  selectedIds.value = newSet
}

// Button text showing selection count
const publishButtonText = computed(() => {
  const count = selectedIds.value.size
  if (count === 0) return 'Publish Instructions'
  if (count === 1) return 'Publish 1 Instruction'
  return `Publish ${count} Instructions`
})

// Initialize selectedIds when drafts load (select all by default)
// Also auto-select any newly arriving drafts (e.g., from SSE streaming)
watch(drafts, (newDrafts, oldDrafts) => {
  const oldIds = new Set((oldDrafts || []).filter(d => d.id).map(d => d.id))
  const newSet = new Set(selectedIds.value)

  // Add any newly appeared drafts to selection
  for (const d of newDrafts) {
    if (d.id && !oldIds.has(d.id)) {
      newSet.add(d.id)
    }
  }

  // Remove any drafts that no longer exist
  for (const id of selectedIds.value) {
    if (!newDrafts.some(d => d.id === id)) {
      newSet.delete(id)
    }
  }

  selectedIds.value = newSet
}, { immediate: true })

// Publish the entire build
const handlePublishBuild = async () => {
  isPublishingBuild.value = true

  try {
    let targetBuildId = buildId.value

    // If no build_id in data, try to fetch it from the first instruction
    if (!targetBuildId && drafts.value[0]?.id) {
      const { data } = await useMyFetch<any>(`/instructions/${drafts.value[0].id}`)
      if (data.value?.current_build_id) {
        targetBuildId = data.value.current_build_id
      }
    }

    if (targetBuildId) {
      // Get the selected instruction IDs
      const selectedInstructionIds = Array.from(selectedIds.value)

      // Update each selected instruction's status to published within the build
      for (const draft of drafts.value) {
        if (!draft.id || !selectedIds.value.has(draft.id)) continue
        await useMyFetch(`/instructions/${draft.id}`, {
          method: 'PUT',
          body: {
            status: 'published',
            target_build_id: targetBuildId,
          }
        })
      }

      // Publish the build with selected instruction IDs (backend will filter out unselected)
      const response = await useMyFetch(`/builds/${targetBuildId}/publish`, {
        method: 'POST',
        body: {
          instruction_ids: selectedInstructionIds,
        }
      })

      if (response.status.value === 'success') {
        localPublishOverride.value = true
        toast.add({ title: 'Success', description: 'Instructions published', color: 'green' })
      } else {
        throw new Error('Failed to publish build')
      }
    } else {
      // Fallback: No build found, just publish selected instructions directly
      for (const draft of drafts.value) {
        if (!draft.id || !selectedIds.value.has(draft.id)) continue
        await useMyFetch(`/instructions/${draft.id}`, {
          method: 'PUT',
          body: { status: 'published' }
        })
      }
      localPublishOverride.value = true
      toast.add({ title: 'Success', description: 'Instructions published', color: 'green' })
    }
  } catch (error) {
    console.error('Error publishing instructions:', error)
    toast.add({ title: 'Error', description: 'Failed to publish instructions', color: 'red' })
  } finally {
    isPublishingBuild.value = false
  }
}

const handleEdit = async (draft: InstructionDraft, _index: number) => {
  // Try to load full instruction (with references/data_sources) before opening modal
  let fullInst: any = null
  try {
    if (draft.id) {
      const { data, error } = await useMyFetch(`/instructions/${draft.id}`)
      if (!error.value) fullInst = data.value
    }
  } catch {}

  const base = fullInst || {
    id: draft.id,
    text: draft.text,
    category: draft.category,
    status: draft.status,
    private_status: draft.private_status,
    global_status: draft.global_status,
    is_seen: draft.is_seen,
    can_user_toggle: draft.can_user_toggle,
    user_id: draft.user_id,
    organization_id: draft.organization_id,
    agent_execution_id: draft.agent_execution_id,
    trigger_reason: draft.trigger_reason,
    created_at: draft.created_at,
    updated_at: draft.updated_at,
    data_sources: [],
    references: []
  }

  editingInstruction.value = base

  // Determine modal type based on permissions
  modalInitialType.value = canCreateInstructions.value ? 'global' : 'private'
  showInstructionModal.value = true
}

const handleRemove = async (draft: InstructionDraft, index: number) => {
  if (!draft.id || !buildId.value) return

  isRemoving.value = true
  removingIndex.value = index

  try {
    // Remove instruction from the build
    const response = await useMyFetch(`/builds/${buildId.value}/contents/${draft.id}`, {
      method: 'DELETE'
    })

    if (response.status.value === 'success') {
      // Update local view
      const rj: any = (props as any).toolExecution?.result_json || {}
      const arr: any[] = Array.isArray(rj.drafts) ? rj.drafts : (Array.isArray(rj.instructions) ? rj.instructions : [])
      if (Array.isArray(arr)) {
        const idx = typeof index === 'number' ? index : arr.findIndex((x: any) => x?.id === draft.id)
        if (idx > -1) {
          arr.splice(idx, 1)
          if (rj.drafts) rj.drafts = [...arr]
          if (rj.instructions) rj.instructions = [...arr]
        }
      }
      toast.add({ title: 'Removed', description: 'Instruction removed from build', color: 'orange' })
    } else {
      throw new Error('Failed to remove instruction')
    }
  } catch (error) {
    console.error('Error removing instruction:', error)
    toast.add({ title: 'Error', description: 'Failed to remove instruction', color: 'red' })
  } finally {
    isRemoving.value = false
    removingIndex.value = -1
  }
}

const handleInstructionSaved = (data: any) => {
  // Sync edited instruction into local drafts array
  try {
    const updated = (data && data.data) ? data.data : data
    const rj: any = (props as any).toolExecution?.result_json || {}
    const arr: any[] = Array.isArray(rj.drafts) ? rj.drafts : (Array.isArray(rj.instructions) ? rj.instructions : [])
    if (updated && updated.id && Array.isArray(arr)) {
      const idx = arr.findIndex((x: any) => x?.id === updated.id)
      if (idx > -1) {
        arr[idx] = { ...(arr[idx] || {}), ...updated }
        if (rj.drafts) rj.drafts = [...arr]
        if (rj.instructions) rj.instructions = [...arr]
      }
    }
  } catch {}
  toast.add({ title: 'Success', description: 'Instruction saved', color: 'green' })
  showInstructionModal.value = false
}

const handleOpenInstruction = (instruction: any) => {
  // Open the clicked instruction in view mode
  editingInstruction.value = instruction
}
</script>

<style scoped>
.markdown-wrapper :deep(.markdown-content) {
  font-size: 14px;
  line-height: 2;
}

@keyframes shimmer {
  0% { background-position: -100% 0; }
  100% { background-position: 100% 0; }
}

.tool-shimmer {
  background: linear-gradient(90deg, #888 0%, #999 25%, #ccc 50%, #999 75%, #888 100%);
  background-size: 200% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: shimmer 2s linear infinite;
  font-weight: 400;
  opacity: 1;
}
</style>
