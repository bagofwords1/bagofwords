<template>
  <UModal v-model="open" :ui="{ width: 'sm:max-w-4xl', height: 'sm:h-[70vh]' }">
    <div class="h-full flex flex-col">
      <!-- Header -->
      <div class="px-4 py-3 border-b flex items-center justify-between flex-shrink-0">
        <div class="text-sm font-medium text-gray-800">Save as Catalog Entity</div>
        <button class="px-3 py-1.5 text-xs rounded border border-gray-300 text-gray-700 hover:bg-gray-50" @click="open = false">Close</button>
      </div>

      <div class="flex-1 flex overflow-hidden min-h-0">
        <!-- Single-pane content -->
        <section class="flex-1 flex flex-col overflow-hidden min-h-0">
          <div class="flex-1 p-4 overflow-auto">
            <div class="grid grid-cols-2 gap-4">
              <div class="col-span-2">
                <label class="block text-xs font-medium text-gray-700 mb-1">Title</label>
                <input v-model="form.title" type="text" class="w-full text-sm border rounded px-2 py-1.5" placeholder="Revenue by month" />
              </div>

              <div class="col-span-2">
                <label class="block text-xs font-medium text-gray-700 mb-1">Description</label>
                <textarea v-model="form.description" rows="6" class="w-full text-sm border rounded px-2 py-2 min-h-[160px]" placeholder="Short description" />
              </div>

              <div class="col-span-2">
                <label class="block text-xs font-medium text-gray-700 mb-1">Status</label>
                <USelectMenu size="xs" v-model="form.status" :options="statusOptions" option-attribute="label" value-attribute="value" class="w-full text-xs">
                  <template #label>
                    <span class="text-xs">{{ form.status }}</span>
                  </template>
                </USelectMenu>
              </div>
            </div>
          </div>

          <!-- Footer Actions -->
          <div class="px-4 py-3 border-t flex items-center justify-end gap-2 flex-shrink-0">
            <button class="px-3 py-1.5 text-xs rounded border border-gray-300 text-gray-700 hover:bg-gray-50" @click="open = false">Cancel</button>
            <button class="px-3 py-1.5 text-xs rounded bg-gray-800 text-white hover:bg-gray-700 disabled:opacity-60" :disabled="saving || !canSave" @click="onSave">
              <span v-if="saving">Saving…</span>
              <span v-else>Save Entity</span>
            </button>
          </div>
        </section>
      </div>
    </div>
  </UModal>
  
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useMyFetch } from '~/composables/useMyFetch'

interface Props {
  visible: boolean
  stepId?: string | null
  initialTitle?: string
  initialView?: Record<string, any> | null
  initialData?: any
  dataModel?: any
  initialCode?: string
  editorLang?: string
}

const props = defineProps<Props>()
const emit = defineEmits(['close', 'saved'])

const open = computed({
  get: () => props.visible,
  set: (v: boolean) => { if (!v) emit('close') }
})

const errorMsg = ref('')
const saving = ref(false)
const viewType = computed(() => String((props.initialView && props.initialView.type) || ''))

const form = ref<{ 
  type: string
  title: string
  description: string | null
  code: string
  data: Record<string, any>
  view: Record<string, any> | null
  status: string
}>({
  type: (viewType.value === 'count' ? 'metric' : 'model'),
  title: props.initialTitle || '',
  description: null,
  code: props.initialCode || '',
  data: props.initialData || {},
  view: props.initialView ? JSON.parse(JSON.stringify(props.initialView)) : null,
  status: 'draft',
})

const statusOptions = [
  { label: 'draft', value: 'draft' },
  { label: 'published', value: 'published' },
]

// No preview; backend uses the step to get code/data

function slugify(s: string): string {
  return (s || '')
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, '')
    .trim()
    .replace(/\s+/g, '-')
    .replace(/-+/g, '-')
}

const canSave = computed(() => !!props.stepId)

async function onSave() {
  saving.value = true
  errorMsg.value = ''
  try {
    if (!props.stepId) throw new Error('Step is required to save')
    const publish = form.value.status === 'published'
    const body: any = {
      title: form.value.title || '',
      description: form.value.description || null,
      publish,
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


