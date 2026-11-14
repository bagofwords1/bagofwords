<template>
  <UPopover v-if="canEdit || folders.length">
    <UButton variant="ghost" color="gray" size="xs" icon="i-heroicons-folder" :aria-label="$t('sharedFolders.title')">
      {{ folders.map(f => f.name).join(', ') || $t('sharedFolders.title') }}
    </UButton>
    <template #panel>
      <div class="w-64 p-2">
        <UInput v-model="search" icon="i-heroicons-magnifying-glass" :placeholder="$t('common.search')" />
        <ul class="mt-1 max-h-60 overflow-auto">
          <li v-for="row in choices" :key="row.id">
            <label class="flex items-center gap-2 px-2 py-1.5 text-xs">
              <input type="checkbox" :checked="ids.has(row.id)" :disabled="busy || !row.canCurate || (!ids.has(row.id) && !canEdit)" @change="change(row, ($event.target as HTMLInputElement).checked)" />
              <span class="break-words">{{ row.name }}</span>
            </label>
          </li>
        </ul>
        <p v-if="error" role="alert" class="p-2 text-xs text-red-600">{{ $t('common.error') }}</p>
      </div>
    </template>
  </UPopover>
</template>
<script setup lang="ts">
type Folder = {id:string, name:string, parent_id?:string, capabilities?:string[]}
const props = defineProps<{id:string, folders: Folder[], directory: Folder[], canEdit:boolean}>()
const emit = defineEmits<{changed: []}>()
const search = ref(''), busy = ref(false), error = ref(false)
const ids = computed(() => new Set(props.folders.map(f => f.id)))
const choices = computed(() => {
  const byId = new Map(props.directory.map(f => [f.id, f]))
  return props.directory.filter(f => ids.value.has(f.id) || (props.canEdit && f.capabilities?.includes('manage_instructions'))).map(f => {
    const path = [f.name]
    let parent = f.parent_id
    while (parent && byId.has(parent)) {
      const node = byId.get(parent)!
      path.unshift(node.name); parent = node.parent_id
    }
    return {id:f.id, name:path.join(' / '), canCurate:f.capabilities?.includes('manage_instructions')}
  }).filter(f => f.name.toLowerCase().includes(search.value.toLowerCase())).sort((a,b) => a.name.localeCompare(b.name))
})
async function change(folder: {id:string}, attach:boolean) {
  busy.value = true
  try {
    const result = await useMyFetch('/folders/' + folder.id + '/knowledge/instructions/' + props.id, {method:attach ? 'PUT' : 'DELETE'})
    error.value = !!result.error.value
    if (!error.value) emit('changed')
  } finally { busy.value = false }
}
</script>
