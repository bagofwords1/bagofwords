<template>
  <UModal v-model="open" :ui="{ width: 'sm:max-w-2xl', height: 'sm:h-[80vh]' }">
    <div class="h-full flex flex-col bg-gray-50">
      <!-- Header -->
      <div class="px-4 py-3 bg-white border-b flex items-center justify-between flex-shrink-0">
        <div class="text-sm font-medium text-gray-800">
          {{ canCreateEntities ? 'Save Query' : 'Suggest Query' }}
        </div>
        <button class="text-xs text-gray-500 hover:text-gray-700" @click="open = false">Close</button>
      </div>

      <div class="flex-1 flex overflow-hidden min-h-0">
        <!-- Single-pane content -->
        <section class="flex-1 flex flex-col overflow-hidden min-h-0">
          <div class="flex-1 overflow-auto">
            <div class="bg-white rounded-lg p-3">
              <!-- Info message for non-admins (suggestions) -->
              <div v-if="!canCreateEntities && canSuggestEntities" class="mb-4 p-3 bg-blue-50 border border-blue-200 rounded-lg text-xs text-blue-800">
                <div class="font-medium mb-1">Suggest Saved Query for Review</div>
                <div>Your query will be submitted for admin approval before being published.</div>
              </div>

              <!-- Info message for admins -->
              <div v-if="canCreateEntities" class="mb-4 p-3 bg-green-50 border border-green-200 rounded-lg text-xs text-green-800">
                <div class="font-medium mb-1">Save Query</div>
                <div>As an admin, your query will be published directly.</div>
              </div>
              
              <!-- Error message for no permissions -->
              <div v-if="!canCreateEntities && !canSuggestEntities" class="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-xs text-red-800">
                <div class="font-medium mb-1">No Permission</div>
                <div>You don't have permission to create or suggest entities.</div>
              </div>
              <EntityForm v-model="form" :show-status="canCreateEntities" />
            </div>
          </div>

          <!-- Footer Actions -->
          <div class="px-4 py-3 bg-white border-t flex items-center justify-end gap-2 flex-shrink-0">
            <button class="bg-white border border-gray-300 rounded-lg px-3 py-1.5 text-xs hover:bg-gray-50" @click="open = false">Cancel</button>
            <button 
              class="text-white text-xs font-medium py-1.5 px-3 rounded-lg disabled:opacity-50" 
              :class="canCreateEntities ? 'bg-blue-500 hover:bg-blue-600' : 'bg-amber-500 hover:bg-amber-600'"
              :disabled="saving || !canSave" 
              @click="onSave"
            >
              <span v-if="saving">{{ canCreateEntities ? 'Saving...' : 'Submitting...' }}</span>
              <span v-else>{{ canCreateEntities ? 'Save Entity' : 'Suggest Entity' }}</span>
            </button>
          </div>
        </section>
      </div>
    </div>
  </UModal>
  
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useMyFetch } from '~/composables/useMyFetch'
import { useCan } from '~/composables/usePermissions'
import EntityForm from './EntityForm.vue'

interface Props {
  visible: boolean
  stepId?: string | null
  initialTitle?: string
  initialView?: Record<string, any> | null
  initialData?: any
  dataModel?: any
  initialCode?: string
  editorLang?: string
  initialDataSourceIds?: string[]
}

const props = defineProps<Props>()
const emit = defineEmits(['close', 'saved'])

const open = computed({
  get: () => props.visible,
  set: (v: boolean) => { if (!v) emit('close') }
})

const canCreateEntities = computed(() => useCan('create_entities'))
const canSuggestEntities = computed(() => useCan('suggest_entities'))
const errorMsg = ref('')
const saving = ref(false)
const viewType = computed(() => String((props.initialView && props.initialView.type) || ''))

const form = ref<{
  type: string
  title: string
  description: string | null
  status: string
  data_source_ids?: string[]
}>({
  type: (viewType.value === 'count' ? 'metric' : 'model'),
  title: props.initialTitle || '',
  description: null,
  status: canCreateEntities.value ? 'published' : 'draft',
  data_source_ids: props.initialDataSourceIds || [],
})

// Watch for modal opening and update form with latest props
watch(() => props.visible, (isVisible) => {
  if (isVisible) {
    // Reset/update form when modal opens
    form.value = {
      type: (viewType.value === 'count' ? 'metric' : 'model'),
      title: props.initialTitle || '',
      description: null,
      status: canCreateEntities.value ? 'published' : 'draft',
      data_source_ids: props.initialDataSourceIds || [],
    }
  }
})

// No preview; backend uses the step to get code/data

function slugify(s: string): string {
  return (s || '')
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, '')
    .trim()
    .replace(/\s+/g, '-')
    .replace(/-+/g, '-')
}

const canSave = computed(() => !!props.stepId && (canCreateEntities.value || canSuggestEntities.value))

async function onSave() {
  saving.value = true
  errorMsg.value = ''
  try {
    if (!props.stepId) throw new Error('Step is required to save')
    
    // If user can create entities, respect their status choice
    // If user can only suggest, force it to be a suggestion (draft)
    const publish = canCreateEntities.value ? (form.value.status === 'published') : false
    
    const body: any = {
      type: form.value.type || 'model',
      title: form.value.title || '',
      description: form.value.description || null,
      publish,
      data_source_ids: form.value.data_source_ids || [],
    }
    
    const { data, error } = await useMyFetch(`/api/entities/from_step/${props.stepId}`, { method: 'POST', body })
    if (error.value) throw error.value
    emit('saved', data.value)
    open.value = false
  } catch (e: any) {
    errorMsg.value = e?.data?.detail || e?.message || 'Failed to save entity'
  } finally {
    saving.value = false
  }
}

</script>

<style scoped>
</style>


