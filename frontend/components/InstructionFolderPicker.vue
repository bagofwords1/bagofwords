<template>
  <UPopover v-if="canEdit || folders.length">
    <UButton variant="ghost" color="gray" size="xs" icon="i-heroicons-folder" :aria-label="$t('sharedFolders.title')">
      {{ folders.map(f => f.name).join(', ') || $t('sharedFolders.title') }}
    </UButton>
    <template #panel>
      <div class="w-64 p-2">
        <UInput v-model="search" icon="i-heroicons-magnifying-glass" :placeholder="$t('common.search')" />
        <div class="mt-1 max-h-60 overflow-auto">
          <AgentFolderTree :folders="choices" :selected="[...ids]" :search="search" :disabled="busy" :disabled-ids="readOnlyIds"
            @select="id => change({id}, !ids.has(id))" />
        </div>
        <p v-if="error" role="alert" class="p-2 text-xs text-red-600">{{ $t('common.error') }}</p>
      </div>
    </template>
  </UPopover>
</template>
<script setup lang="ts">
import { buildAgentFolderOptions } from '~/composables/useAgentFolderOptions'
type Folder = {id:string, name:string, parent_id?:string, capabilities?:string[]}
const props = defineProps<{id:string, folders: Folder[], directory: Folder[], canEdit:boolean}>()
const emit = defineEmits<{changed: []}>()
const search = ref(''), busy = ref(false), error = ref(false)
const ids = computed(() => new Set(props.folders.map(f => f.id)))
const choices = computed(() => buildAgentFolderOptions(props.directory, f => ids.value.has(f.id) || !!(props.canEdit && f.capabilities?.includes('manage_instructions'))))
const readOnlyIds = computed(() => props.directory.filter(f => !f.capabilities?.includes('manage_instructions')).map(f => f.id))
async function change(folder: {id:string}, attach:boolean) {
  busy.value = true
  try {
    const result = await useMyFetch('/folders/' + folder.id + '/knowledge/instructions/' + props.id, {method:attach ? 'PUT' : 'DELETE'})
    error.value = !!result.error.value
    if (!error.value) emit('changed')
  } finally { busy.value = false }
}
</script>
