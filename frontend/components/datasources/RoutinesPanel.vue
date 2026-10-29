<template>
  <!-- Stored procedures / functions. Mirrors the tables list exactly (rows,
       checkboxes, Select all / Deselect all, Save) so selecting which routines
       the agent sees works just like selecting tables. Renders only when the
       agent's connections have indexed routines. -->
  <div v-if="flat.length" class="mt-6 border-t border-gray-100 dark:border-gray-800 pt-4">
    <!-- Section header: collapsible, with the active ratio + bulk actions. -->
    <div class="flex items-center justify-between gap-2">
      <button type="button" class="flex items-center gap-1.5 text-sm font-semibold text-gray-800 dark:text-gray-200"
        :aria-expanded="open" @click="open = !open">
        <UIcon :name="open ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-4 h-4 text-gray-500 dark:text-gray-400 rtl-flip" />
        {{ $t('routines.title') }}
      </button>
      <div v-if="open && canUpdate" class="flex items-center gap-2">
        <button @click="selectAll" :disabled="saving"
          class="px-2 py-0.5 text-2xs rounded border border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50">Select all</button>
        <button @click="deselectAll" :disabled="saving"
          class="px-2 py-0.5 text-2xs rounded border border-gray-300 dark:border-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50">Deselect all</button>
      </div>
    </div>

    <div v-show="open">
      <div v-if="canUpdate" class="mt-1 text-2xs text-gray-500 dark:text-gray-400">{{ activeCount }}/{{ flat.length }} active</div>
      <p class="mt-1 text-2xs text-gray-500 dark:text-gray-400">{{ $t('routines.note') }}</p>

      <ul class="mt-2 divide-y divide-gray-100 dark:divide-gray-800">
        <li v-for="r in flat" :key="r.id" class="py-2 px-2">
          <div class="flex items-center">
            <UCheckbox v-if="canUpdate" color="blue" :model-value="draftActive.has(r.id)" :disabled="saving"
              @update:model-value="(val: boolean) => toggle(r.id, val)" class="me-3" />
            <button type="button" class="flex items-center justify-between text-start flex-1" @click="expanded[r.id] = !expanded[r.id]">
              <div class="flex items-center min-w-0">
                <UIcon :name="expanded[r.id] ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-4 h-4 me-1 text-gray-500 dark:text-gray-400 rtl-flip" />
                <template v-if="multiConnection">
                  <DataSourceIcon :type="r.connection_type" class="h-3.5 me-1 flex-shrink-0" />
                  <span class="text-2xs px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 me-1.5 flex-shrink-0 truncate max-w-[120px]">{{ r.connection_name }}</span>
                </template>
                <span class="text-2xs px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 me-1.5 flex-shrink-0">
                  {{ r.routine_type === 'function' ? $t('routines.function') : $t('routines.procedure') }}
                </span>
                <span class="text-sm text-gray-800 dark:text-gray-200 truncate">{{ r.name }}</span>
                <span v-if="!draftActive.has(r.id) && canUpdate" class="ms-2 text-2xs px-1 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400">inactive</span>
                <span v-if="isDirty(r.id)" class="ms-1 text-2xs px-1 py-0.5 rounded bg-yellow-100 text-yellow-700">modified</span>
                <span v-if="!r.definition_available" class="ms-2 text-2xs text-amber-600 dark:text-amber-400">{{ $t('routines.notReadable') }}</span>
              </div>
            </button>
          </div>
          <!-- Expanded: the signature, like a table expands to its columns. -->
          <div v-if="expanded[r.id]" class="mt-2 ms-7">
            <div class="border border-gray-100 dark:border-gray-800 rounded px-2 py-1.5">
              <code class="text-xs text-gray-700 dark:text-gray-300 break-all">{{ signature(r) }}</code>
            </div>
          </div>
        </li>
      </ul>
      <!-- No Save button here: the tables' single Save persists routines too
           (TablesSelector.onSave calls this panel's exposed save()). -->
    </div>
  </div>
</template>

<script setup lang="ts">
import DataSourceIcon from '@/components/DataSourceIcon.vue'

interface RoutineParam { name?: string; dtype?: string; mode?: string }
interface Routine {
  id: string
  name: string
  routine_type: string
  arguments?: string | null
  params?: RoutineParam[] | null
  returns?: string | null
  definition_available: boolean
  is_active: boolean
  connection_id: string
  connection_name: string
  connection_type: string
}

const props = withDefaults(defineProps<{ dsId: string; canUpdate?: boolean }>(), { canUpdate: true })
// Lets the tables' single Save surface an "unsaved changes" hint for routines too.
const emit = defineEmits<{ (e: 'dirtyChange', v: boolean): void }>()

const flat = ref<Routine[]>([])
const open = ref(true)
const expanded = ref<Record<string, boolean>>({})
const draftActive = ref<Set<string>>(new Set())
const originalActive = ref<Set<string>>(new Set())
const saving = ref(false)

const multiConnection = computed(() => new Set(flat.value.map((r) => r.connection_id)).size > 1)
const activeCount = computed(() => flat.value.filter((r) => draftActive.value.has(r.id)).length)
const dirty = computed(() =>
  draftActive.value.size !== originalActive.value.size ||
  [...draftActive.value].some((id) => !originalActive.value.has(id)))

function isDirty(id: string): boolean {
  return draftActive.value.has(id) !== originalActive.value.has(id)
}

function signature(r: Routine): string {
  let inside = r.arguments ?? ''
  if (!r.arguments && r.params?.length) {
    inside = r.params.map((p) => [p.name, p.dtype].filter(Boolean).join(' ')).filter(Boolean).join(', ')
  }
  let sig = `${r.routine_type} ${r.name}(${inside})`
  if (r.returns) sig += ` → ${r.returns}`
  return sig
}

function toggle(id: string, val: boolean) {
  const next = new Set(draftActive.value)
  val ? next.add(id) : next.delete(id)
  draftActive.value = next
}
function selectAll() { draftActive.value = new Set(flat.value.map((r) => r.id)) }
function deselectAll() { draftActive.value = new Set() }

async function load() {
  flat.value = []
  if (!props.dsId) return
  const res = await useMyFetch(`/data_sources/${props.dsId}/routines`, { method: 'GET' })
  if (res.error?.value) return
  const groups = (res.data.value as any[]) || []
  const rows: Routine[] = []
  for (const g of groups) {
    for (const r of (g.routines || [])) {
      rows.push({ ...r, connection_id: g.connection_id, connection_name: g.connection_name, connection_type: g.connection_type })
    }
  }
  flat.value = rows
  const active = new Set(rows.filter((r) => r.is_active).map((r) => r.id))
  draftActive.value = new Set(active)
  originalActive.value = new Set(active)
}

async function save() {
  if (!dirty.value) return
  saving.value = true
  try {
    await useMyFetch(`/data_sources/${props.dsId}/routines_status`, {
      method: 'PUT', body: { active_ids: [...draftActive.value] },
    })
    await load()
  } finally {
    saving.value = false
  }
}

watch(() => props.dsId, load, { immediate: true })
watch(dirty, (v) => emit('dirtyChange', v))

// Saved by the tables' single Save (TablesSelector.onSave), not a button here.
defineExpose({ save, hasChanges: dirty })
</script>
