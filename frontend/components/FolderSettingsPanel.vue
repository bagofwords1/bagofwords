<template>
  <section class="mt-5 border-t border-line px-2 pt-4" data-testid="folder-settings">
    <h3 class="mb-4 text-sm font-medium">{{ $t('nav.settings') }}</h3>
    <div class="mb-5 border-b border-gray-100 pb-5 dark:border-gray-800">
      <div class="mb-2 flex items-center justify-between">
        <h4 class="text-2xs font-semibold uppercase tracking-wider text-ink-muted">{{ $t('agentAccess.access') }}</h4>
        <button class="h-7 rounded-lg border border-line px-2.5 text-xs font-medium hover:bg-surface-muted" @click="$emit('share')">{{ $t('agentAccess.addMember') }}</button>
      </div>
      <div class="space-y-1">
        <div v-for="entry in members" :key="entry.type + entry.id" class="flex items-center justify-between gap-3 rounded-md border border-gray-100 px-3 py-2 hover:bg-gray-50 dark:border-gray-800 dark:hover:bg-gray-800/50">
          <div class="flex min-w-0 items-start gap-1.5">
            <UIcon :name="entry.type === 'user' ? 'i-heroicons-user' : entry.type === 'group' ? 'i-heroicons-user-group' : 'i-heroicons-identification'" class="mt-0.5 size-3.5 shrink-0 text-ink-muted" />
            <div><div class="break-all text-xs font-medium">{{ entry.name }}</div>
              <div v-for="grant in entry.inherited" :key="grant.source_id" class="text-2xs text-ink-muted">{{ label(grant.role) }} · {{ $t('sharedFolders.via', {name: grant.source}) }}</div>
            </div>
          </div>
          <UButton v-if="entry.role && entry.type !== 'role'" size="xs" color="white" trailing-icon="i-heroicons-chevron-down-20-solid" @click="$emit('share', entry.name)">{{ label(entry.role) }}</UButton>
          <span v-else-if="entry.role" class="text-xs text-ink-muted">{{ label(entry.role) }}</span>
        </div>
        <p v-if="loadError" role="alert" class="text-xs text-red-600">{{ $t('common.error') }}</p>
        <p v-else-if="ready && !members.length" class="rounded-md border border-line p-3 text-center text-2xs text-ink-muted">{{ $t('agentAccess.noMembers') }}</p>
      </div>
    </div>
    <details>
      <summary class="cursor-pointer text-2xs font-semibold uppercase tracking-wider text-ink-muted">{{ $t('folderRemoval.danger') }}</summary>
      <UButton class="mt-2" color="red" variant="ghost" size="xs" icon="i-heroicons-trash" @click="show = true">{{ $t('sharedFolders.delete') }}</UButton>
    </details>
    <UModal v-model="show" :prevent-close="busy" :ui="{width:'sm:max-w-md'}">
      <form class="space-y-4 p-5" @submit.prevent="remove">
        <h2 class="break-words text-base font-semibold">{{ $t('folderRemoval.title', {name:folder.name}) }}</h2>
        <label v-for="choice in choices" :key="choice" class="flex items-start gap-2 text-sm">
          <input v-model="mode" type="radio" class="mt-1" :value="choice" :disabled="busy" />
          <span>{{ $t('folderRemoval.' + choice) }}<small class="block text-xs text-ink-muted">{{ $t('folderRemoval.' + choice + 'Hint') }}</small></span>
        </label>
        <label v-if="mode === 'keep'" class="block text-xs text-ink-muted" for="folder-removal-destination">{{ $t('sharedFolders.to') }}</label>
        <USelect id="folder-removal-destination" :placeholder="$t('sharedFolders.chooseDestination')" v-if="mode === 'keep'" v-model="destination" :options="destinations" option-attribute="label" value-attribute="id" :aria-label="$t('sharedFolders.to')" :disabled="busy" />
        <p v-if="error" role="alert" class="text-xs text-red-600">{{ error }}</p>
        <div class="flex justify-end gap-2"><UButton color="gray" variant="ghost" :disabled="busy" @click="show = false">{{ $t('common.cancel') }}</UButton>
          <UButton color="red" type="submit" :loading="busy" :disabled="mode === 'keep' && !destinations.some(d => d.id === destination)">{{ $t('sharedFolders.delete') }}</UButton></div>
      </form>
    </UModal>
  </section>
</template>
<script setup lang="ts">
type Folder = {id:string, name:string, parent_id?:string | null, can_manage?:boolean}
type Member = {id:string, type:string, name:string, role?:string, inherited?:{source_id:string, source:string, role:string}[]}
const props = defineProps<{folder:Folder, folders:Folder[], revision:number, hasKnowledge:boolean}>()
const emit = defineEmits<{share:[name?:string], deleted:[]}>()
const {t} = useI18n()
const label = (role:string) => t(role === 'Custom' ? 'rolesManager.levels.custom' : 'sharedFolders.' + role.toLowerCase())
const members = ref<Member[]>([]), ready = ref(false), loadError = ref(false)
watch(() => [props.folder.id, props.revision], async (_, __, cleanup) => {
  let current = true
  cleanup(() => { current = false })
  ready.value = false
  const {data,error} = await useMyFetch<Member[]>('/folders/' + props.folder.id + '/audience')
  if (!current) return
  members.value = (data.value || []).filter(m => m.role || m.inherited?.length)
  loadError.value = !!error.value
  ready.value = true
}, {immediate:true})
const show = ref(false), busy = ref(false), error = ref(''), mode = ref<'keep'|'contents'>('keep')
const choices = ['keep','contents'] as const
const destination = ref('')
const destinations = computed(() => {
  const options = buildAgentFolderOptions(props.folders, f => !!f.can_manage)
    .filter(f => !f.ancestors.includes(props.folder.id)).map(f => ({id:f.id,label:f.path}))
  return props.folder.parent_id || props.hasKnowledge ? options : [{id:'',label:t('sharedFolders.root')}, ...options]
})
watch(show, open => { if(open) { mode.value='keep'; error.value=''; destination.value=props.folder.parent_id || '' } })
async function remove() {
  if (busy.value) return
  busy.value = true
  try {
    const response = await useMyFetch('/folders/' + props.folder.id, {method:'DELETE', query:{
      mode:mode.value, destination_id:mode.value === 'keep' && destination.value ? destination.value : undefined,
    }})
    if(response.error.value) { error.value = response.error.value.data?.detail || t('common.error'); return }
    show.value = false
    emit('deleted')
  } finally {busy.value = false}
}
</script>
