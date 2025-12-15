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
      <div v-for="(d, i) in drafts" :key="i"  class="hover:bg-gray-50 border border-gray-150 rounded-md p-3 transition-colors">
        <div 
          @click="d.global_status === 'approved' || d.global_status === 'rejected' ? null : handleEdit(d, i)" 
          :class="[
            'text-[12px] text-gray-800 leading-relaxed mb-2',
            d.global_status === 'approved' || d.global_status === 'rejected' ? '' : 'cursor-pointer'
          ]"
        >
          {{ d.text }}
        </div>
        <div v-if="d.category" class="text-xs hidden text-gray-500 mb-3 font-medium">{{ d.category }}</div>
        
        <!-- Action buttons / Status display -->
        <div class="flex justify-start gap-2 pt-2 border-t border-gray-200">
          <!-- Show status for approved instructions -->
          <div v-if="d.global_status === 'approved'" class="flex items-center">
            <Icon name="heroicons:check-circle" class="w-3 h-3 text-gray-500 mr-1" />
            <span class="text-[10px] font-medium text-gray-500">Published</span>
          </div>
          
          <!-- Show status for rejected instructions -->
          <div v-else-if="d.global_status === 'rejected'" class="flex items-center">
            <Icon name="heroicons:x-circle" class="w-3 h-3 text-gray-500 mr-1" />
            <span class="text-[10px] font-medium text-gray-500">Rejected</span>
          </div>
          
          <!-- Show action buttons for suggested/draft instructions -->
          <template v-else>
            <button 
              v-if="canCreateInstructions"
              @click="handlePublish(d, i)"
              class="flex items-center text-[10px] font-medium text-gray-700 bg-gray-50 hover:bg-gray-100 rounded transition-colors"
              :disabled="isPublishing"
            >
              <Spinner 
                v-if="isPublishing && publishingIndex === i"
                class="w-3 h-3 text-green-600 mr-1" 
              />
              <Icon 
                v-else
                name="heroicons:check" 
                class="w-3 h-3 text-green-600 mr-1" 
              />
              <span>{{ isPublishing && publishingIndex === i ? 'Publishing...' : 'Publish' }}</span>
            </button>
            
            <button 
              @click="handleEdit(d, i)"
              class="flex items-center text-[10px] font-medium text-gray-700 bg-gray-50 hover:bg-gray-100 rounded transition-colors"
            >
              <Icon name="heroicons:pencil" class="w-3 h-3 text-blue-600 mr-1" />
              <span>Edit</span>
            </button>
            
            <button 
              @click="handleDelete(d, i)"
              class="flex items-center text-[10px] font-medium text-gray-700 bg-gray-50 hover:bg-gray-100 rounded transition-colors"
              :disabled="isRejecting"
            >
              <Spinner 
                v-if="isRejecting && rejectingIndex === i"
                class="w-3 h-3 text-red-600 mr-1" 
              />
              <Icon 
                v-else
                name="heroicons:trash" 
                class="w-3 h-3 text-red-600 mr-1" 
              />
              <span>{{ isRejecting && rejectingIndex === i ? 'Deleting...' : 'Delete' }}</span>
            </button>
          </template>
        </div>
      </div>
    </div>
    
    <!-- Instruction Modal -->
    <InstructionModalComponent
      v-model="showInstructionModal"
      :instruction="editingInstruction"
      :initial-type="modalInitialType"
      :is-suggestion="true"
      @instruction-saved="handleInstructionSaved"
    />
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
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
}

interface Props {
  toolExecution: ToolExecution
}

const props = defineProps<Props>()

// Reactive state
const showInstructionModal = ref(false)
const editingInstruction = ref<any>(null)
const modalInitialType = ref<'global' | 'private'>('private')
const isPublishing = ref(false)
const isRejecting = ref(false)
const publishingIndex = ref(-1)
const rejectingIndex = ref(-1)

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
      updated_at: d?.updated_at || null
    })).filter(d => d.text)
  }
  return []
})

const isLoading = computed(() => {
  return props.toolExecution?.status === 'running' || props.toolExecution?.status === 'in_progress'
})

const canCreateInstructions = computed(() => {
  return useCan('create_instructions')
})

// Action handlers
const handlePublish = async (draft: InstructionDraft, index: number) => {
  isPublishing.value = true
  publishingIndex.value = index
  
  try {
    if (!draft.id) throw new Error('Missing instruction id')
    // Approve existing suggestion → published global
    const response = await useMyFetch(`/instructions/${draft.id}`, {
      method: 'PUT',
      body: {
        status: 'published',
        global_status: 'approved'
      }
    })

    if (response.status.value === 'success') {
      // Update local view so status chip renders immediately
      const rj: any = (props as any).toolExecution?.result_json || {}
      const arr: any[] = Array.isArray(rj.drafts) ? rj.drafts : (Array.isArray(rj.instructions) ? rj.instructions : [])
      if (Array.isArray(arr)) {
        const idx = typeof index === 'number' ? index : arr.findIndex((x: any) => x?.id === draft.id)
        if (idx > -1) {
          arr[idx] = { ...(arr[idx] || {}), status: 'published', global_status: 'approved' }
          if (rj.drafts) rj.drafts = [...arr]
          if (rj.instructions) rj.instructions = [...arr]
        }
      }
      toast.add({ title: 'Success', description: 'Instruction published', color: 'green' })
    } else {
      throw new Error('Failed to publish instruction')
    }
  } catch (error) {
    console.error('Error publishing instruction:', error)
    toast.add({ title: 'Error', description: 'Failed to publish instruction', color: 'red' })
  } finally {
    isPublishing.value = false
    publishingIndex.value = -1
  }
}

const handleEdit = async (draft: InstructionDraft, index: number) => {
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

const handleDelete = async (draft: InstructionDraft, index: number) => {
  isRejecting.value = true
  rejectingIndex.value = index
  
  try {
    if (!draft.id) throw new Error('Missing instruction id')
    const response = await useMyFetch(`/instructions/${draft.id}`, { method: 'DELETE' })

    if (response.status.value === 'success') {
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
      toast.add({ title: 'Deleted', description: 'Instruction deleted', color: 'orange' })
    } else {
      throw new Error('Failed to delete instruction')
    }
  } catch (error) {
    console.error('Error deleting instruction:', error)
    toast.add({ title: 'Error', description: 'Failed to delete instruction', color: 'red' })
  } finally {
    isRejecting.value = false
    rejectingIndex.value = -1
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


