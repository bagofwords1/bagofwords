<template>
  <UModal v-model="open" :ui="{ width: 'sm:max-w-2xl', height: 'sm:h-[80vh]' }">
    <div class="h-full flex flex-col bg-gray-50 dark:bg-gray-900">
      <!-- Header -->
      <div class="px-4 py-3 bg-white dark:bg-gray-900 border-b flex items-center justify-between flex-shrink-0">
        <div class="text-sm font-medium text-gray-800 dark:text-gray-200">
          {{ canCreateEntities ? $t('entityCreate.saveQuery') : $t('entityCreate.suggestQuery') }}
        </div>
        <button class="text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300" @click="open = false">{{ $t('entityCreate.close') }}</button>
      </div>

      <div class="flex-1 flex overflow-hidden min-h-0">
        <!-- Single-pane content -->
        <section class="flex-1 flex flex-col overflow-hidden min-h-0">
          <div class="flex-1 overflow-auto">
            <div class="bg-white dark:bg-gray-900 rounded-lg p-3">
              <!-- Info message for non-admins (suggestions) -->
              <div v-if="!canCreateEntities && canSuggestEntities" class="mb-4 p-3 bg-blue-50 dark:bg-blue-950 border border-blue-200 rounded-lg text-xs text-blue-800">
                <div class="font-medium mb-1">{{ $t('entityCreate.suggestHeading') }}</div>
                <div>{{ $t('entityCreate.suggestBody') }}</div>
              </div>

              <!-- Info message for admins -->
              <div v-if="canCreateEntities" class="mb-4 p-3 bg-green-50 dark:bg-green-950 border border-green-200 rounded-lg text-xs text-green-800">
                <div class="font-medium mb-1">{{ $t('entityCreate.adminHeading') }}</div>
                <div>{{ $t('entityCreate.adminBody') }}</div>
              </div>

              <!-- Error message for no permissions -->
              <div v-if="!canCreateEntities && !canSuggestEntities" class="mb-4 p-3 bg-red-50 dark:bg-red-950 border border-red-200 rounded-lg text-xs text-red-800">
                <div class="font-medium mb-1">{{ $t('entityCreate.noPermissionHeading') }}</div>
                <div>{{ $t('entityCreate.noPermissionBody') }}</div>
              </div>
              <!-- Save failure: surface the backend's reason instead of failing silently -->
              <div v-if="errorMsg" class="mb-4 p-3 bg-red-50 dark:bg-red-950 border border-red-200 rounded-lg text-xs text-red-800">
                <div class="font-medium mb-1">{{ $t('entityCreate.saveFailed') }}</div>
                <div>{{ errorMsg }}</div>
              </div>
              <EntityForm v-model="form" :show-status="canCreateEntities" />
            </div>
          </div>

          <!-- Footer Actions -->
          <div class="px-4 py-3 bg-white dark:bg-gray-900 border-t flex items-center justify-end gap-2 flex-shrink-0">
            <button class="bg-white dark:bg-gray-900 border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-1.5 text-xs hover:bg-gray-50 dark:hover:bg-gray-800" @click="open = false">{{ $t('entityCreate.cancel') }}</button>
            <button
              class="text-white text-xs font-medium py-1.5 px-3 rounded-lg disabled:opacity-50"
              :class="canCreateEntities ? 'bg-blue-500 hover:bg-blue-600' : 'bg-amber-500 hover:bg-amber-600'"
              :disabled="saving || !canSave"
              @click="onSave"
            >
              <span v-if="saving">{{ canCreateEntities ? $t('entityCreate.saving') : $t('entityCreate.submitting') }}</span>
              <span v-else>{{ canCreateEntities ? $t('entityCreate.saveEntity') : $t('entityCreate.suggestEntity') }}</span>
            </button>
          </div>
        </section>
      </div>
    </div>
  </UModal>

</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useMyFetch } from '~/composables/useMyFetch'
import { useCanAll, useCanAny } from '~/composables/usePermissions'
import EntityForm from './EntityForm.vue'

const { t } = useI18n()
const toast = useToast()

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
  status: 'draft', // set on open by the watcher below (needs canCreateEntities)
  data_source_ids: props.initialDataSourceIds || [],
})

// Publishing needs per-agent `create_entities` on EVERY attached agent
// (agent owners hold it via their `manage` grant; org `manage_entities` and
// full admins via implication). There is no org-level 'create_entities'
// permission — the old org-level check locked out agent owners entirely.
const canCreateEntities = computed(() => {
  const ids = (form.value?.data_source_ids || []).filter(Boolean)
  return ids.length
    ? useCanAll('create_entities', 'data_source', ids)
    : useCanAny('create_entities', 'data_source')
})
// Any member may SUGGEST an entity (draft pending admin approval) from their
// own query; the backend enforces access to the attached agents.
const canSuggestEntities = computed(() => true)

// Keep the Status field truthful: the publish verdict depends on the selected
// agents, which can change after the modal opens (async report snapshot, user
// edits the Data Sources select). If the verdict drops to suggest-tier while
// Status still says Published, the save would silently downgrade to a
// suggestion — reset it so the UI always shows what will actually happen.
watch(canCreateEntities, (can) => {
  if (!can && form.value.status === 'published') form.value.status = 'draft'
})

// Watch for modal opening and update form with latest props
const { getSession } = useAuth()
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
    // The permission store is a session snapshot; grants earned mid-session
    // (e.g. `manage` on an agent the user just created) aren't in it yet and
    // would wrongly downgrade the modal to suggest-tier. Refreshing the
    // session re-resolves the store (fetchPermissions plugin watches it), and
    // the computeds above re-evaluate reactively.
    getSession().then(() => {
      if (canCreateEntities.value && form.value.status !== 'published') {
        form.value.status = 'published'
      }
    }).catch(() => {})
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
    if (!props.stepId) throw new Error(t('entityCreate.stepRequired'))

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
    // Announce what actually happened — published to catalog vs suggestion
    // pending review — so a save never ends without visible feedback.
    const saved: any = data.value
    toast.add({
      title: saved?.global_status === 'approved' && saved?.status === 'published'
        ? t('entityCreate.publishedToast')
        : t('entityCreate.suggestedToast'),
      color: 'green',
    })
    emit('saved', saved)
    open.value = false
  } catch (e: any) {
    errorMsg.value = e?.data?.detail || e?.message || t('entityCreate.saveFailed')
    toast.add({ title: t('entityCreate.saveFailed'), description: errorMsg.value, color: 'red' })
  } finally {
    saving.value = false
  }
}

</script>

<style scoped>
</style>

