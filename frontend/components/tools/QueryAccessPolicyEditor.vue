<template>
  <div class="h-full overflow-y-auto p-4">
    <!-- Enterprise gate. Authoring needs the license; a saved policy keeps
         enforcing without it, so this never blocks turning one OFF. -->
    <div v-if="!licensed" data-testid="qap-upgrade"
         class="max-w-xl mx-auto my-8 rounded-lg border border-amber-300 bg-amber-50 dark:bg-amber-950/40 dark:border-amber-800 p-5">
      <div class="flex items-center gap-2 font-medium text-amber-800 dark:text-amber-300">
        <UIcon name="heroicons-sparkles" class="w-5 h-5" />
        Row & column security is an Enterprise feature
      </div>
      <p class="text-sm text-amber-700 dark:text-amber-400 mt-2">
        Filter this query per viewer — by profile attribute from Entra or Google,
        group, or role — so each person opening the report or its artifact sees
        only their own rows, and sensitive columns stay hidden. Available with an
        enterprise license.
      </p>
    </div>

    <div v-else-if="loading" class="flex flex-col items-center py-12">
      <Spinner class="w-4 h-4 mb-2" />
      <div class="text-gray-400 text-sm">Loading policy…</div>
    </div>

    <div v-else-if="!resultColumns.length" data-testid="qap-no-columns"
         class="text-sm text-gray-500 dark:text-gray-400 py-12 text-center">
      Run this query once first — a policy filters on its result columns.
    </div>

    <div v-else class="flex gap-5">
      <!-- ── Left: the rules ───────────────────────────────────────────── -->
      <div class="w-1/2 space-y-4">
        <!-- Rows -->
        <div>
          <label class="flex items-start gap-2 cursor-pointer">
            <input data-testid="qap-rls-enabled" type="checkbox" v-model="rls.enabled" class="mt-0.5" />
            <span>
              <span class="text-xs font-medium text-gray-700 dark:text-gray-200">Filter rows per viewer</span>
              <span class="block text-[11px] text-gray-500 dark:text-gray-400">
                Each person who opens this report sees only the rows their
                identity allows. You still see everything while editing.
              </span>
            </span>
          </label>

          <div v-if="rls.enabled" class="mt-3 pl-6 space-y-3">
            <div>
              <label class="block text-[11px] text-gray-500 dark:text-gray-400 mb-1">Filter on column</label>
              <select data-testid="qap-rls-column" v-model="rls.column"
                      class="w-full border border-gray-200 dark:border-gray-700 rounded-md px-2 py-1.5 text-xs bg-white dark:bg-gray-900">
                <option value="">Choose a column…</option>
                <option v-for="c in resultColumns" :key="c" :value="c">{{ c }}</option>
              </select>
            </div>

            <div>
              <label class="block text-[11px] text-gray-500 dark:text-gray-400 mb-1">Match against</label>
              <select data-testid="qap-rls-source" v-model="rls.source"
                      class="w-full border border-gray-200 dark:border-gray-700 rounded-md px-2 py-1.5 text-xs bg-white dark:bg-gray-900">
                <option value="">Choose an identity attribute…</option>
                <option value="user.email">Their email address</option>
                <option value="groups">Their groups</option>
                <option value="roles">Their roles</option>
                <option v-for="k in options.attribute_keys" :key="k" :value="`profile.${k}`">
                  Profile · {{ k }}
                </option>
              </select>
              <p v-if="!options.attribute_keys?.length" class="text-[10px] text-gray-400 mt-1">
                No directory profile attributes are synced yet. Entra / Google
                attributes appear here once members have signed in through the
                identity provider.
              </p>
            </div>

            <div>
              <div class="flex items-center justify-between mb-1">
                <label class="text-[11px] text-gray-500 dark:text-gray-400">Extra grants</label>
                <button data-testid="qap-rls-add-grant" class="text-[11px] text-blue-600 hover:underline"
                        @click="addRowGrant">+ Add grant</button>
              </div>
              <p class="text-[10px] text-gray-400 mb-1.5">
                Give a person, group or role specific values — or <code>*</code> to see every row.
              </p>
              <div v-for="(g, i) in rls.grants" :key="i" class="flex items-center gap-1.5 mb-1.5">
                <select :data-testid="`qap-rls-grant-type-${i}`" v-model="g.principal_type"
                        class="border border-gray-200 dark:border-gray-700 rounded px-1.5 py-1 text-[11px] bg-white dark:bg-gray-900">
                  <option value="user">User</option>
                  <option value="group">Group</option>
                  <option value="role">Role</option>
                </select>
                <select :data-testid="`qap-rls-grant-principal-${i}`" v-model="g.principal_id"
                        class="flex-1 min-w-0 border border-gray-200 dark:border-gray-700 rounded px-1.5 py-1 text-[11px] bg-white dark:bg-gray-900">
                  <option value="">Choose…</option>
                  <option v-for="p in principalsFor(g.principal_type)" :key="p.id" :value="p.id">{{ p.name }}</option>
                </select>
                <input :data-testid="`qap-rls-grant-values-${i}`" v-model="g.valuesText"
                       placeholder="EMEA, APAC or *"
                       class="w-28 border border-gray-200 dark:border-gray-700 rounded px-1.5 py-1 text-[11px] bg-white dark:bg-gray-900" />
                <button :data-testid="`qap-rls-grant-remove-${i}`" class="text-gray-400 hover:text-red-600"
                        @click="rls.grants.splice(i, 1)">
                  <UIcon name="heroicons-x-mark" class="w-3.5 h-3.5" />
                </button>
              </div>
            </div>

            <label class="flex items-start gap-2 cursor-pointer">
              <input data-testid="qap-rls-default-deny" type="checkbox" v-model="rls.default_deny" class="mt-0.5" />
              <span class="text-[11px] text-gray-600 dark:text-gray-300">
                Show nothing when the attribute can't be resolved
                <span class="block text-gray-400">
                  Recommended. With this off, a viewer whose attribute is missing
                  sees every row.
                </span>
              </span>
            </label>
          </div>
        </div>

        <div class="border-t border-gray-100 dark:border-gray-800 pt-4">
          <!-- Columns -->
          <label class="flex items-start gap-2 cursor-pointer">
            <input data-testid="qap-cls-enabled" type="checkbox" v-model="cls.enabled" class="mt-0.5" />
            <span>
              <span class="text-xs font-medium text-gray-700 dark:text-gray-200">Restrict columns</span>
              <span class="block text-[11px] text-gray-500 dark:text-gray-400">
                Listed columns are blanked out for anyone without a grant.
                Every other column stays visible.
              </span>
            </span>
          </label>

          <div v-if="cls.enabled" class="mt-3 pl-6 space-y-2">
            <div class="flex items-center justify-between">
              <label class="text-[11px] text-gray-500 dark:text-gray-400">Restricted columns</label>
              <button data-testid="qap-cls-add" class="text-[11px] text-blue-600 hover:underline"
                      @click="addRestrictedColumn">+ Restrict a column</button>
            </div>
            <div v-for="(c, i) in cls.columns" :key="i"
                 class="border border-gray-200 dark:border-gray-700 rounded-md p-2 space-y-1.5">
              <div class="flex items-center gap-1.5">
                <select :data-testid="`qap-cls-column-${i}`" v-model="c.name"
                        class="flex-1 border border-gray-200 dark:border-gray-700 rounded px-1.5 py-1 text-[11px] bg-white dark:bg-gray-900">
                  <option value="">Choose a column…</option>
                  <option v-for="col in resultColumns" :key="col" :value="col">{{ col }}</option>
                </select>
                <button :data-testid="`qap-cls-remove-${i}`" class="text-gray-400 hover:text-red-600"
                        @click="cls.columns.splice(i, 1)">
                  <UIcon name="heroicons-x-mark" class="w-3.5 h-3.5" />
                </button>
              </div>
              <div class="flex items-center justify-between">
                <span class="text-[10px] text-gray-400">Visible to</span>
                <button :data-testid="`qap-cls-add-grant-${i}`" class="text-[10px] text-blue-600 hover:underline"
                        @click="c.grants.push({ principal_type: 'group', principal_id: '' })">+ Grant</button>
              </div>
              <div v-for="(g, j) in c.grants" :key="j" class="flex items-center gap-1.5">
                <select v-model="g.principal_type"
                        class="border border-gray-200 dark:border-gray-700 rounded px-1.5 py-1 text-[11px] bg-white dark:bg-gray-900">
                  <option value="user">User</option>
                  <option value="group">Group</option>
                  <option value="role">Role</option>
                </select>
                <select :data-testid="`qap-cls-grant-${i}-${j}`" v-model="g.principal_id"
                        class="flex-1 min-w-0 border border-gray-200 dark:border-gray-700 rounded px-1.5 py-1 text-[11px] bg-white dark:bg-gray-900">
                  <option value="">Choose…</option>
                  <option v-for="p in principalsFor(g.principal_type)" :key="p.id" :value="p.id">{{ p.name }}</option>
                </select>
                <button class="text-gray-400 hover:text-red-600" @click="c.grants.splice(j, 1)">
                  <UIcon name="heroicons-x-mark" class="w-3.5 h-3.5" />
                </button>
              </div>
              <p v-if="!c.grants.length" class="text-[10px] text-amber-600">
                No grants — this column is hidden from everyone but you.
              </p>
            </div>
          </div>
        </div>

        <p v-if="error" data-testid="qap-error" class="text-[11px] text-red-600">{{ error }}</p>
        <div class="flex items-center gap-2">
          <button data-testid="qap-save"
                  class="px-3 py-1.5 text-xs rounded bg-gray-800 text-white hover:bg-gray-700 disabled:opacity-50"
                  :disabled="saving" @click="save">
            <Spinner v-if="saving" class="w-3.5 h-3.5 inline" />
            <span v-else>Save policy</span>
          </button>
          <span v-if="savedAt" data-testid="qap-saved" class="text-[11px] text-green-600">Saved</span>
        </div>
      </div>

      <!-- ── Right: preview as a real person ───────────────────────────── -->
      <div class="w-1/2 border-l border-gray-100 dark:border-gray-800 pl-5">
        <div class="text-xs font-medium text-gray-700 dark:text-gray-200 mb-1">Check it</div>
        <p class="text-[11px] text-gray-500 dark:text-gray-400 mb-2">
          Runs the query exactly as that person's dashboard would, using the
          rules above — saved or not.
        </p>
        <div class="flex items-center gap-1.5">
          <select data-testid="qap-as-user" v-model="previewUserId"
                  class="flex-1 min-w-0 border border-gray-200 dark:border-gray-700 rounded-md px-2 py-1.5 text-xs bg-white dark:bg-gray-900">
            <option value="">Preview as…</option>
            <option v-for="m in options.members" :key="m.id" :value="m.id">{{ m.name }}</option>
          </select>
          <button data-testid="qap-preview"
                  class="px-2.5 py-1.5 text-xs rounded border border-gray-200 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50"
                  :disabled="!previewUserId || previewing" @click="runPreview">
            <Spinner v-if="previewing" class="w-3.5 h-3.5 inline" />
            <span v-else>Preview</span>
          </button>
        </div>
        <p v-if="previewError" class="text-[11px] text-red-600 mt-2">{{ previewError }}</p>

        <div v-if="preview" class="mt-3">
          <div data-testid="qap-verdict" class="rounded-md px-2.5 py-2 text-[11px] mb-2"
               :class="preview.denied
                 ? 'bg-red-50 text-red-700 dark:bg-red-950/40 dark:text-red-300'
                 : (preview.filtered || preview.masked_columns?.length)
                   ? 'bg-blue-50 text-blue-700 dark:bg-blue-950/40 dark:text-blue-300'
                   : 'bg-gray-50 text-gray-600 dark:bg-gray-800 dark:text-gray-300'">
            <template v-if="preview.denied">
              Sees <b>no rows</b> — {{ preview.reason }}.
            </template>
            <template v-else-if="preview.filtered">
              Sees <b>{{ preview.row_count }}</b> row(s) where <b>{{ preview.column }}</b>
              is <b>{{ preview.allowed_values.join(', ') }}</b>.
            </template>
            <template v-else>
              Sees <b>every row</b> — {{ preview.reason }}.
            </template>
            <span v-if="preview.masked_columns?.length" class="block mt-0.5">
              Hidden column(s): <b>{{ preview.masked_columns.join(', ') }}</b>.
            </span>
          </div>

          <!-- The identity the rules resolved against. Without this a creator
               debugging an empty preview cannot tell a wrong rule from a
               member whose directory attributes never synced. -->
          <div data-testid="qap-identity" class="text-[10px] text-gray-500 dark:text-gray-400 mb-2 space-y-0.5">
            <div><span class="text-gray-400">email</span> {{ preview.identity?.email || '—' }}</div>
            <div><span class="text-gray-400">groups</span> {{ preview.identity?.groups?.join(', ') || '—' }}</div>
            <div><span class="text-gray-400">roles</span> {{ preview.identity?.roles?.join(', ') || '—' }}</div>
            <div v-for="(v, k) in (preview.identity?.profile_attributes || {})" :key="k">
              <span class="text-gray-400">{{ k }}</span> {{ v }}
            </div>
          </div>

          <div class="border border-gray-200 dark:border-gray-700 rounded-md overflow-auto max-h-64">
            <table class="min-w-full text-[11px]">
              <thead class="bg-gray-50 dark:bg-gray-800 sticky top-0">
                <tr>
                  <th v-for="c in preview.columns" :key="c.field || c.headerName"
                      class="text-left px-2 py-1 font-medium text-gray-600 dark:text-gray-300 whitespace-nowrap">
                    {{ c.headerName || c.field }}
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(r, i) in preview.rows" :key="i" class="border-t border-gray-100 dark:border-gray-800">
                  <td v-for="c in preview.columns" :key="c.field"
                      class="px-2 py-1 whitespace-nowrap"
                      :class="r[c.field] === null || r[c.field] === undefined ? 'text-gray-300 italic' : ''">
                    {{ r[c.field] === null || r[c.field] === undefined ? 'hidden' : r[c.field] }}
                  </td>
                </tr>
                <tr v-if="!preview.rows.length">
                  <td :colspan="Math.max(preview.columns.length, 1)"
                      class="px-2 py-3 text-center text-gray-400">No rows</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import Spinner from '../Spinner.vue'
import { useMyFetch } from '~/composables/useMyFetch'

const props = defineProps<{ queryId?: string | null; visible: boolean }>()

const { hasFeature } = useEnterprise()
const licensed = computed(() => hasFeature('rls'))

const loading = ref(false)
const saving = ref(false)
const savedAt = ref(false)
const error = ref('')

const options = ref<any>({ columns: [], attribute_keys: [], members: [], groups: [], roles: [] })
const resultColumns = computed<string[]>(() => options.value.columns || [])

// `valuesText` is the comma-separated string the input binds to; it is split
// into the array the API expects only on save. Editing the array directly
// would fight the user on every keystroke that leaves a trailing comma.
const rls = reactive<any>({ enabled: false, column: '', source: '', grants: [], default_deny: true })
const cls = reactive<any>({ enabled: false, columns: [] })

const previewUserId = ref('')
const previewing = ref(false)
const preview = ref<any | null>(null)
const previewError = ref('')

function principalsFor(type: string) {
  if (type === 'user') return options.value.members || []
  if (type === 'role') return options.value.roles || []
  return options.value.groups || []
}

function addRowGrant() {
  rls.grants.push({ principal_type: 'group', principal_id: '', valuesText: '' })
}

function addRestrictedColumn() {
  cls.columns.push({ name: '', grants: [] })
}

function hydrate(data: any) {
  options.value = data || {}
  const p = data?.rls_policy || {}
  rls.enabled = !!data?.rls_enabled
  rls.column = p.column || ''
  rls.source = p.source || ''
  rls.default_deny = data?.rls_default_deny !== false
  rls.grants = (p.grants || []).map((g: any) => ({
    principal_type: g.principal_type || 'group',
    principal_id: g.principal_id || '',
    valuesText: (g.values || []).join(', '),
  }))
  cls.enabled = !!data?.cls_enabled
  cls.columns = ((data?.cls_policy || {}).columns || []).map((c: any) => ({
    name: c.name || '',
    grants: (c.grants || []).map((g: any) => ({
      principal_type: g.principal_type || 'group',
      principal_id: g.principal_id || '',
    })),
  }))
}

function buildPayload() {
  return {
    rls_enabled: rls.enabled,
    rls_mode: 'attribute',
    rls_policy: rls.enabled
      ? {
          column: rls.column,
          source: rls.source,
          op: 'in',
          grants: rls.grants
            .filter((g: any) => g.principal_id)
            .map((g: any) => ({
              principal_type: g.principal_type,
              principal_id: g.principal_id,
              values: String(g.valuesText || '')
                .split(',')
                .map((s: string) => s.trim())
                .filter(Boolean),
            })),
        }
      : null,
    rls_default_deny: rls.default_deny,
    cls_enabled: cls.enabled,
    cls_policy: cls.enabled
      ? {
          columns: cls.columns
            .filter((c: any) => c.name)
            .map((c: any) => ({
              name: c.name,
              grants: c.grants.filter((g: any) => g.principal_id),
            })),
        }
      : null,
  }
}

async function load() {
  if (!props.queryId || !licensed.value) return
  loading.value = true
  error.value = ''
  try {
    const data: any = await useMyFetch(`/api/queries/${props.queryId}/access-policy`)
    hydrate(data?.data?.value ?? data)
  } catch (e: any) {
    error.value = e?.data?.detail || 'Could not load the policy.'
  } finally {
    loading.value = false
  }
}

async function save() {
  if (!props.queryId) return
  saving.value = true
  error.value = ''
  savedAt.value = false
  try {
    const res: any = await useMyFetch(`/api/queries/${props.queryId}/access-policy`, {
      method: 'PUT',
      body: buildPayload(),
    })
    const data = res?.data?.value ?? res
    if (data?.detail) throw { data }
    hydrate(data)
    savedAt.value = true
    setTimeout(() => (savedAt.value = false), 2500)
  } catch (e: any) {
    error.value = e?.data?.detail || 'Could not save the policy.'
  } finally {
    saving.value = false
  }
}

async function runPreview() {
  if (!props.queryId || !previewUserId.value) return
  previewing.value = true
  previewError.value = ''
  preview.value = null
  try {
    const res: any = await useMyFetch(`/api/queries/${props.queryId}/access-policy/preview`, {
      method: 'POST',
      body: { target_user_id: previewUserId.value, ...buildPayload() },
    })
    const data = res?.data?.value ?? res
    if (data?.detail) throw { data }
    preview.value = data
  } catch (e: any) {
    previewError.value = e?.data?.detail || 'Preview failed.'
  } finally {
    previewing.value = false
  }
}

watch(() => [props.visible, props.queryId], ([v]) => { if (v) load() }, { immediate: true })
</script>
