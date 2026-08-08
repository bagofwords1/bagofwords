<template>
  <!-- Trigger setup modal.
       There is no create-vs-edit mode: opening "New trigger" provisions an
       inactive trigger up front so its delivery URL exists immediately and
       stays visible at the top — the URL is what the user actually came for,
       and pasting it into the sending service is step one. -->
  <!-- The dialog is sized once and its own surface is left bare (the card
       inside draws it), so the narrower editing width can live on the card.
       Driving UModal's `ui.width` off viewMode instead restarts its enter
       transition when the mode flips, leaving the panel stuck at opacity 0. -->
  <UModal v-model="isOpen" :ui="{ width: 'w-full sm:max-w-4xl', background: '', rounded: '', shadow: '' }">
    <UCard
      :class="viewMode ? '' : 'sm:max-w-2xl sm:mx-auto'"
      :ui="{ body: { padding: 'px-5 py-4 sm:p-5' }, header: { padding: 'px-5 py-3 sm:px-5 sm:py-3' } }"
    >
      <template #header>
        <div class="flex items-start justify-between gap-3">
          <div class="min-w-0">
            <h3 class="text-sm font-semibold text-gray-900 dark:text-white">
              {{ viewMode ? $t('triggers.viewTitle') : $t('triggers.setupTitle') }}
            </h3>
            <p class="text-xs text-gray-400 mt-0.5">{{ $t('triggers.modalSubtitle') }}</p>
          </div>
          <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" size="xs" @click="showModal = false" />
        </div>
      </template>

      <!-- Two columns while viewing: the trigger on the left, the sessions it
           spawned and its provenance on the right. -->
      <div :class="viewMode ? 'grid grid-cols-1 md:grid-cols-5 gap-5' : ''">
      <div :class="viewMode ? 'md:col-span-3 min-w-0' : ''">

      <!-- Name: a heading when viewing, a field when editing -->
      <div v-if="viewMode" class="text-sm font-medium text-gray-900 dark:text-white mb-4" data-testid="trigger-view-name">
        {{ form.name || $t('triggers.namePlaceholder') }}
      </div>
      <div v-else class="mb-4">
        <div class="text-xs text-gray-500 dark:text-gray-400 mb-1.5">{{ $t('triggers.name') }}</div>
        <input
          v-model="form.name"
          type="text"
          maxlength="120"
          :placeholder="$t('triggers.namePlaceholder')"
          data-testid="trigger-name"
          class="w-full text-sm border border-gray-200 dark:border-gray-700 rounded px-3 py-2 bg-white dark:bg-gray-900 dark:text-gray-100 dark:placeholder-gray-500 focus:outline-none focus:ring-1 focus:ring-blue-500 focus:border-blue-500"
        />
      </div>

      <!-- ── 1. The hook: where events come from (both modes — the URL is
                the whole point of a trigger) ─────────────────────────────── -->
      <section data-testid="trigger-endpoint-section">
        <h4 class="text-xs font-semibold text-gray-700 dark:text-gray-300">{{ $t('triggers.endpointSection') }}</h4>
        <p class="text-[11px] text-gray-400 mt-0.5 mb-2">{{ $t('triggers.endpointHint') }}</p>

        <div class="flex items-center gap-2 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 px-3 py-2">
          <UIcon name="heroicons-link" class="w-3.5 h-3.5 text-gray-400 shrink-0" />
          <code class="flex-1 text-xs text-gray-700 dark:text-gray-300 truncate" data-testid="trigger-url">{{ current?.delivery_url || '—' }}</code>
          <button
            class="shrink-0 inline-flex items-center gap-1 text-[11px] px-2 py-1 rounded border border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400 hover:bg-white dark:hover:bg-gray-800"
            data-testid="trigger-copy-url"
            @click="copy(current?.delivery_url, 'url')"
          >
            <UIcon :name="copied === 'url' ? 'heroicons-check' : 'heroicons-clipboard-document'" class="w-3 h-3" />
            {{ copied === 'url' ? $t('triggers.copied') : $t('triggers.copy') }}
          </button>
        </div>

        <!-- In secret-URL mode the URL is the credential, so rotating it is
             the only meaningful revocation. -->
        <button
          v-if="form.auth_mode === 'url_token'"
          class="mt-1.5 text-[11px] text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 inline-flex items-center gap-1"
          data-testid="trigger-rotate-url"
          @click="rotate(current)"
        >
          <UIcon name="heroicons-arrow-path" class="w-3 h-3" /> {{ $t('triggers.rotateUrl') }}
        </button>

        <!-- Auth mode + what it means for the sender -->
        <div class="mt-2 flex items-start gap-2">
          <span v-if="viewMode" class="shrink-0 text-xs text-gray-700 dark:text-gray-300 pt-1.5">{{ authModeLabel }}</span>
          <select v-else v-model="form.auth_mode" data-testid="trigger-auth"
            class="shrink-0 rounded-lg border border-gray-200 dark:border-gray-700 px-2 py-1.5 text-xs text-gray-800 dark:text-gray-200 bg-white dark:bg-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-100 focus:border-blue-300">
            <option value="url_token">{{ $t('triggers.authUrlToken') }}</option>
            <option value="token">{{ $t('triggers.authToken') }}</option>
            <option value="hmac">{{ $t('triggers.authHmac') }}</option>
          </select>
          <p class="text-[11px] text-gray-400 leading-snug pt-1.5">{{ authHint }}</p>
        </div>

        <!-- Signing secret: only meaningful for the header/HMAC modes, and only
             available in the session that minted it (create or rotate). -->
        <div v-if="form.auth_mode !== 'url_token'" class="mt-2">
          <div v-if="sessionSecret" class="flex items-center gap-2 rounded-lg border border-green-200 dark:border-green-900 bg-green-50/50 dark:bg-green-950/30 px-3 py-2">
            <span class="text-[10px] font-medium uppercase tracking-wide text-gray-400 shrink-0">{{ $t('triggers.secret') }}</span>
            <code class="flex-1 text-xs text-gray-700 dark:text-gray-300 truncate" data-testid="trigger-secret">{{ sessionSecret }}</code>
            <button class="shrink-0 text-gray-400 hover:text-gray-700 dark:hover:text-gray-300" @click="copy(sessionSecret, 'secret')">
              <UIcon :name="copied === 'secret' ? 'heroicons-check' : 'heroicons-clipboard-document'" class="w-4 h-4" />
            </button>
          </div>
          <p v-if="sessionSecret" class="mt-1 text-[11px] text-gray-400">{{ $t('triggers.copyOnce') }}</p>
          <button v-else class="text-[11px] text-blue-500 hover:text-blue-600 inline-flex items-center gap-1" data-testid="trigger-rotate" @click="rotate(current)">
            <UIcon name="heroicons-arrow-path" class="w-3 h-3" /> {{ $t('triggers.rotate') }}
          </button>
        </div>

        <!-- Live delivery status: the feedback loop that was missing — paste the
             URL, hit the sender's test button, watch it land here. -->
        <div class="mt-2.5 flex items-center gap-2 text-[11px]" data-testid="trigger-event-status">
          <template v-if="lastEvent">
            <UIcon name="heroicons-check-circle-solid" class="w-3.5 h-3.5 text-green-500 shrink-0" />
            <span class="text-gray-600 dark:text-gray-300 truncate">
              {{ $t('triggers.eventReceived', { time: formatRelativeTime(lastEvent.received_at) }) }}
              <span v-if="lastEvent.summary" class="text-gray-400">— {{ lastEvent.summary }}</span>
            </span>
            <button class="shrink-0 text-blue-500 hover:text-blue-600" data-testid="trigger-toggle-payload" @click="showPayload = !showPayload">
              {{ showPayload ? $t('triggers.hidePayload') : $t('triggers.viewPayload') }}
            </button>
          </template>
          <template v-else>
            <Spinner class="w-3 h-3 shrink-0" />
            <span class="text-gray-400">{{ $t('triggers.waitingForEvent') }}</span>
          </template>
        </div>

        <div v-if="showPayload && lastEvent" class="mt-2 rounded-lg border border-gray-200 dark:border-gray-800 overflow-hidden" data-testid="trigger-payload">
          <div v-if="lastEvent.headers && Object.keys(lastEvent.headers).length" class="px-3 py-2 border-b border-gray-100 dark:border-gray-800">
            <div class="text-[10px] font-medium uppercase tracking-wide text-gray-400 mb-1">{{ $t('triggers.headers') }}</div>
            <div v-for="(v, k) in lastEvent.headers" :key="k" class="text-[11px] font-mono text-gray-500 dark:text-gray-400 truncate">
              <span class="text-gray-400">{{ k }}:</span> {{ v }}
            </div>
          </div>
          <div class="px-3 py-2">
            <div class="text-[10px] font-medium uppercase tracking-wide text-gray-400 mb-1">{{ $t('triggers.body') }}</div>
            <pre class="text-[11px] font-mono text-gray-600 dark:text-gray-300 whitespace-pre-wrap break-all max-h-48 overflow-y-auto">{{ prettyPayload }}</pre>
          </div>
        </div>
      </section>

      <!-- ── Read-only run spec ────────────────────────────────────────── -->
      <section v-if="viewMode" class="mt-5 pt-4 border-t border-gray-100 dark:border-gray-800">
        <h4 class="text-xs font-semibold text-gray-700 dark:text-gray-300 mb-2">{{ $t('triggers.taskSection') }}</h4>
        <div class="rounded-lg border border-gray-100 dark:border-gray-800 bg-gray-50/60 dark:bg-gray-900/40 px-3 py-2 text-sm text-gray-700 dark:text-gray-300 whitespace-pre-wrap" dir="auto" data-testid="trigger-view-task">
          {{ current?.task_template || $t('triggers.noTask') }}
        </div>
        <dl class="mt-4 space-y-2.5">
          <div class="flex items-start gap-2">
            <dt class="w-24 shrink-0 text-xs text-gray-400">{{ $t('scheduledPrompt.agents') }}</dt>
            <dd class="text-xs text-gray-700 dark:text-gray-300" data-testid="trigger-view-agents">
              <span v-if="(current?.data_sources || []).length">{{ current.data_sources.map((d) => d.name).join(', ') }}</span>
              <span v-else class="text-gray-400">{{ $t('triggers.noAgents') }}</span>
            </dd>
          </div>
          <div class="flex items-start gap-2">
            <dt class="w-24 shrink-0 text-xs text-gray-400">{{ $t('scheduledPrompt.model') }}</dt>
            <dd class="text-xs text-gray-700 dark:text-gray-300">{{ modelName(current?.model_id) }}</dd>
          </div>
          <div class="flex items-start gap-2">
          </div>
          <div class="flex items-start gap-2">
            <dt class="w-24 shrink-0 text-xs text-gray-400">{{ $t('scheduledPrompt.project') }}</dt>
            <dd class="text-xs text-gray-700 dark:text-gray-300">
              <NuxtLink v-if="current?.project_id" :to="`/projects/${current.project_id}`" class="text-blue-500 hover:text-blue-600" @click="showModal = false">
                {{ current.project_name || $t('triggers.project') }}
              </NuxtLink>
              <span v-else class="text-gray-400">{{ $t('triggers.noProjectShort') }}</span>
            </dd>
          </div>
          <div class="flex items-start gap-2">
            <dt class="w-24 shrink-0 text-xs text-gray-400">{{ $t('triggers.filterSection2') }}</dt>
            <dd class="text-xs text-gray-700 dark:text-gray-300">
              {{ current?.classify_enabled ? $t('triggers.filterOn') : $t('triggers.filterOff') }}
            </dd>
          </div>
          <div class="flex items-center gap-2">
            <dt class="w-24 shrink-0 text-xs text-gray-400">{{ $t('triggers.activeToggle') }}</dt>
            <dd>
              <button
                data-testid="trigger-view-active"
                class="relative inline-flex h-4 w-7 items-center rounded-full transition-colors disabled:opacity-50"
                :class="form.is_active ? 'bg-blue-500' : 'bg-gray-300 dark:bg-gray-700'"
                :aria-pressed="form.is_active"
                :disabled="saving"
                @click="toggleActiveInPlace"
              >
                <span class="inline-block h-3 w-3 rounded-full bg-white transition-transform" :class="form.is_active ? 'translate-x-3.5' : 'translate-x-0.5'" />
              </button>
            </dd>
          </div>
        </dl>
      </section>

      <!-- ── 2. What the agent should do ───────────────────────────────── -->
      <section v-if="!viewMode" class="mt-5 pt-4 border-t border-gray-100 dark:border-gray-800" data-testid="trigger-task-box">
        <h4 class="text-xs font-semibold text-gray-700 dark:text-gray-300 mb-2">{{ $t('triggers.taskSection') }}</h4>
        <PromptBoxV2
          v-if="showModal"
          ref="promptBoxRef"
          :key="current?.id || 'new'"
          :initialSelectedDataSources="initialDataSources"
          :initialMode="current?.mode || 'chat'"
          :initialModel="current?.model_id || ''"
          :textareaContent="current?.task_template || ''"
          :hideScheduleButton="true"
          :hideSubmitButton="true"
          :flush="true"
          :rows="5"
          :projectSelectable="true"
          :project="currentProjectForBox"
        />
      </section>

      <!-- ── 3. Which events are worth a run ───────────────────────────── -->
      <section v-if="!viewMode" class="mt-5 pt-4 border-t border-gray-100 dark:border-gray-800">
        <h4 class="text-xs font-semibold text-gray-700 dark:text-gray-300 mb-2">{{ $t('triggers.filterSection') }}</h4>
        <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer">
          <input v-model="form.classify_enabled" type="checkbox" data-testid="trigger-classify" class="rounded border-gray-300 dark:border-gray-600 text-blue-600 focus:ring-blue-200" />
          {{ $t('triggers.classifierToggle') }}
        </label>
        <div v-if="form.classify_enabled" class="mt-2">
          <label class="block text-xs text-gray-500 dark:text-gray-400 mb-1.5">{{ $t('triggers.classifierGuidance') }}</label>
          <textarea v-model="form.classifier_prompt" rows="2" :placeholder="$t('triggers.classifierPlaceholder')"
            class="w-full rounded-lg border border-gray-200 dark:border-gray-700 px-3 py-2 text-sm text-gray-800 dark:text-gray-200 dark:bg-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-100 focus:border-blue-300"></textarea>
        </div>
      </section>

      <!-- Active toggle: same inline control as the scheduled-task modal, so
           the trigger's state is unambiguous while you edit it. -->
      <div v-if="!viewMode" class="mt-5 pt-4 border-t border-gray-100 dark:border-gray-800 flex items-center justify-between">
        <span class="text-xs text-gray-500 dark:text-gray-400">{{ $t('triggers.activeToggle') }}</span>
        <button
          data-testid="trigger-active"
          class="relative inline-flex h-4 w-7 items-center rounded-full transition-colors"
          :class="form.is_active ? 'bg-blue-500' : 'bg-gray-300 dark:bg-gray-700'"
          :aria-pressed="form.is_active"
          @click="form.is_active = !form.is_active"
        >
          <span class="inline-block h-3 w-3 rounded-full bg-white transition-transform" :class="form.is_active ? 'translate-x-3.5' : 'translate-x-0.5'" />
        </button>
      </div>
      </div>

      <!-- ── Sessions this trigger spawned + provenance (view only) ─────── -->
      <aside v-if="viewMode" class="md:col-span-2 min-w-0 md:border-s md:ps-5 border-gray-100 dark:border-gray-800" data-testid="trigger-runs-column">
        <div class="flex items-baseline justify-between">
          <h4 class="text-[11px] font-semibold uppercase tracking-wider text-gray-400">{{ $t('scheduledPrompt.previousRuns') }}</h4>
          <span v-if="current?.run_count" class="text-[11px] text-gray-300 dark:text-gray-600">{{ current.run_count }}</span>
        </div>

        <div v-if="runsLoading" class="mt-2 text-[11px] text-gray-400 inline-flex items-center">
          <Spinner class="me-1 w-3 h-3" /> {{ $t('triggers.loading') }}
        </div>
        <ul v-else-if="runs.length" class="mt-1.5 -mx-2">
          <li v-for="run in runs" :key="run.report_id">
            <NuxtLink
              :to="`/reports/${run.report_id}`"
              class="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-gray-50 dark:hover:bg-gray-800/60"
              @click="showModal = false"
            >
              <AutomationsRunStatusDot :report-id="run.report_id" :status="run.status" />
              <span class="flex-1 min-w-0 truncate text-[12px] text-gray-700 dark:text-gray-300">{{ run.event_summary || run.title }}</span>
              <span class="shrink-0 text-[10px] text-gray-400">{{ formatRunDate(run.created_at) }}</span>
            </NuxtLink>
          </li>
        </ul>
        <p v-else class="mt-2 text-[11px] text-gray-400">{{ $t('scheduledPrompt.noRuns') }}</p>

        <dl class="mt-5 pt-4 border-t border-gray-100 dark:border-gray-800 space-y-2">
          <!-- No "last delivery" row when the runs above already date the
               most recent one; it matters when deliveries arrived but never
               produced a session (declined, paused, or not yet configured). -->
          <div v-if="!runs.length && current?.last_delivery_at" class="flex items-start gap-2">
            <dt class="w-20 shrink-0 text-[11px] text-gray-400">{{ $t('triggers.lastDeliveryLabel') }}</dt>
            <dd class="text-[11px] text-gray-600 dark:text-gray-300">{{ formatRunDate(current.last_delivery_at) }}</dd>
          </div>
          <div class="flex items-start gap-2">
            <dt class="w-20 shrink-0 text-[11px] text-gray-400">{{ $t('scheduledPrompt.created') }}</dt>
            <dd class="text-[11px] text-gray-600 dark:text-gray-300">{{ formatRunDate(current?.created_at) }}</dd>
          </div>
          <div v-if="current?.user_name" class="flex items-start gap-2">
            <dt class="w-20 shrink-0 text-[11px] text-gray-400">{{ $t('scheduledPrompt.createdBy') }}</dt>
            <dd class="text-[11px] text-gray-600 dark:text-gray-300 truncate">{{ current.user_name }}</dd>
          </div>
          <!-- Deliveries execute as the owner, with their agent access and
               quota — worth stating next to who created it. -->
          <div v-if="current?.user_name" class="flex items-start gap-2">
            <dt class="w-20 shrink-0 text-[11px] text-gray-400">{{ $t('triggers.runsAs') }}</dt>
            <dd class="text-[11px] text-gray-600 dark:text-gray-300 truncate">{{ current.user_name }}</dd>
          </div>
        </dl>
      </aside>
      </div>

      <template #footer>
        <div class="flex items-center justify-between gap-2">
          <UButton
            color="red"
            variant="ghost"
            size="xs"
            icon="i-heroicons-trash"
            data-testid="trigger-delete"
            @click="removeTrigger(current)"
          >{{ $t('triggers.delete') }}</UButton>
          <div class="flex justify-end gap-2">
            <UButton color="gray" variant="ghost" size="xs" @click="onCancel">{{ $t('triggers.cancel') }}</UButton>
            <UButton v-if="viewMode" color="blue" size="xs" icon="i-heroicons-pencil-square" data-testid="trigger-edit" @click="viewMode = false">
              {{ $t('scheduledPrompt.edit') }}
            </UButton>
            <UButton v-else color="blue" size="xs" :loading="saving" data-testid="trigger-save" @click="save()">
              {{ $t('triggers.save') }}
            </UButton>
          </div>
        </div>
      </template>
    </UCard>
  </UModal>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import PromptBoxV2 from '~/components/prompt/PromptBoxV2.vue'

const toast = useToast()
const { t } = useI18n()

const props = defineProps<{
  // The trigger to open, read-only first. `null` means create: the modal
  // provisions one as soon as it opens, which is what makes the delivery URL
  // available from the first second — pasting it into the sending service is
  // the user's actual first step.
  trigger?: any | null
  // Where a newly created trigger is filed. The prompt box shows it as a chip
  // and the user can still move it from there before saving.
  project?: { id: string; name: string; color?: string | null } | null
  // Enabled models, when the caller already loaded them.
  models?: any[]
}>()

// `changed` covers create, save, rotate, pause and delete — the caller just
// refetches whatever list it is showing.
const emit = defineEmits(['changed'])

const showModal = defineModel<boolean>({ default: false })

// The trigger open in the modal. Always a persisted row — see `trigger` above.
const current = ref<any | null>(null)

// The dialog itself only opens once there is a trigger to show: a create
// provisions one first, and its delivery URL is the first thing on screen.
// (Rendering an interim placeholder instead would swap UModal's child mid
// transition, which leaves the card stuck at opacity 0.)
const isOpen = computed({
  get: () => showModal.value && !!current.value,
  set: (v: boolean) => { if (!v) showModal.value = false },
})
// Signing secret for the hmac/token modes. Held in memory only for the session
// that minted it (creation or rotation) — the server never returns it again.
const sessionSecret = ref<string | null>(null)
// True until a freshly provisioned trigger has been saved at least once, so an
// abandoned empty draft can be cleaned up on close.
const isUnsavedDraft = ref(false)
const saving = ref(false)
const copied = ref<string | null>(null)
const showPayload = ref(false)
// An existing trigger opens read-only; a freshly provisioned one goes straight
// to the form (there is nothing to look at yet).
const viewMode = ref(false)
const runs = ref<any[]>([])
const runsLoading = ref(false)
const promptBoxRef = ref<InstanceType<typeof PromptBoxV2> | null>(null)
const models = ref<any[]>(props.models || [])
let pollTimer: any = null

const { formatDateTime } = useFormatDate()
const { relativeTime: formatRelativeTime } = useRelativeTime()
const { fetchActivity } = useReportActivity()

function formatRunDate(v?: string) { return v ? formatDateTime(v) : '' }

// Receiving/gate config kept as a plain form; the run spec (task text,
// agents, mode, model) lives in the embedded PromptBoxV2 (standalone mode —
// no report_id) and is read back via its exposed getters on save.
const defaultForm = () => ({
  name: '',
  source: 'generic',
  // Secret-URL by default: most senders (Intercom, Zapier, cron/curl) can only
  // be given a URL, so a header-token default silently 401s every delivery.
  auth_mode: 'url_token',
  classify_enabled: false,
  classifier_prompt: '',
  is_active: true,
})
const form = ref(defaultForm())

// Agents pre-selected in the prompt box for the open trigger.
const initialDataSources = computed(() => (current.value?.data_sources || []).map((d: any) => ({ ...d })))

const lastEvent = computed(() => current.value?.last_event || null)

// Seeds the prompt box's project chip: the trigger's own binding once it has
// one, otherwise the project the modal was opened from.
const currentProjectForBox = computed(() =>
  current.value?.project_id
    ? { id: current.value.project_id, name: current.value.project_name || props.project?.name || '', color: props.project?.color }
    : (props.project || null)
)

const authHint = computed(() => {
  const mode = form.value.auth_mode
  if (mode === 'token') return t('triggers.authHintToken')
  if (mode === 'hmac') return t('triggers.authHintHmac')
  return t('triggers.authHintUrlToken')
})

const authModeLabel = computed(() => {
  const mode = form.value.auth_mode
  if (mode === 'token') return t('triggers.authToken')
  if (mode === 'hmac') return t('triggers.authHmac')
  return t('triggers.authUrlToken')
})

const prettyPayload = computed(() => {
  const raw = lastEvent.value?.raw
  if (raw === undefined || raw === null) return ''
  try { return typeof raw === 'string' ? raw : JSON.stringify(raw, null, 2) } catch { return String(raw) }
})

function modelName(id: string | null): string {
  if (!id) return t('triggers.defaultModel')
  const m = models.value.find((m: any) => m.id === id)
  return m?.name || t('triggers.defaultModel')
}

function hydrateForm(trig: any) {
  form.value = {
    name: trig.name === 'Trigger' ? '' : (trig.name || ''),
    source: trig.source,
    auth_mode: trig.auth_mode,
    classify_enabled: trig.classify_enabled,
    classifier_prompt: trig.classifier_prompt || '',
    is_active: trig.is_active,
  }
}

async function fetchModels() {
  if (models.value.length) return
  try {
    const { data } = await useMyFetch('/llm/models?is_enabled=true')
    models.value = ((data.value as any[]) || []).filter((m: any) => m.is_enabled !== false)
  } catch { models.value = [] }
}
watch(() => props.models, (m) => { if (m?.length) models.value = m })

// Provision the trigger a "new trigger" click is about to configure. Active
// from the start: the URL works immediately, and a trigger with no task yet
// only records deliveries (the backend skips the run when there is nothing to
// do), so nothing fires before it is configured. `project_id` files it into
// the project the modal was opened from — the sessions it spawns land there.
async function provisionDraft() {
  try {
    const { data, error } = await useMyFetch('/triggers', {
      method: 'POST',
      body: { name: '', auth_mode: 'url_token', is_active: true, project_id: props.project?.id || null },
    })
    if (error.value || !data.value) throw error.value || new Error('create failed')
    const created = data.value as any
    current.value = created
    sessionSecret.value = created.secret || null
    isUnsavedDraft.value = true
    hydrateForm(created)
    showPayload.value = false
    viewMode.value = false
    runs.value = []
    startPolling()
  } catch (e) {
    console.error('create trigger draft failed', e)
    toast.add({ title: t('common.error'), description: t('triggers.saveFailed'), color: 'red' })
    showModal.value = false
  }
}

// Sessions this trigger spawned, for the summary's right-hand column.
async function fetchRuns(triggerId: string) {
  runsLoading.value = true
  runs.value = []
  try {
    const { data } = await useMyFetch(`/triggers/${triggerId}/runs`)
    runs.value = (data.value as any)?.runs || []
    // Track these sessions so a run still executing shows a spinner rather
    // than the verdict of its previous turn.
    fetchActivity(runs.value.map((r: any) => r.report_id))
  } catch { runs.value = [] } finally { runsLoading.value = false }
}

// Poll the open trigger so a delivery that lands while the user is configuring
// shows up without a manual refresh — this is the feedback loop that tells them
// their URL actually works.
function startPolling() {
  stopPolling()
  pollTimer = setInterval(async () => {
    if (!current.value?.id) return
    try {
      const { data } = await useMyFetch(`/triggers/${current.value.id}`)
      if (data.value) {
        const fresh = data.value as any
        // Refresh server-owned fields only; never clobber in-flight edits.
        current.value = { ...current.value, last_event: fresh.last_event, run_count: fresh.run_count, last_delivery_at: fresh.last_delivery_at }
      }
    } catch { /* transient — keep polling */ }
  }, 3000)
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}
onBeforeUnmount(stopPolling)

// Opening either shows the trigger the caller passed or provisions a new one.
watch(showModal, async (open) => {
  if (!open) { await onClosed(); return }
  fetchModels()
  if (props.trigger) {
    current.value = props.trigger
    sessionSecret.value = null
    isUnsavedDraft.value = false
    hydrateForm(props.trigger)
    showPayload.value = false
    viewMode.value = true
    fetchRuns(props.trigger.id)
    startPolling()
  } else {
    current.value = null
    await provisionDraft()
  }
})

// Closing decides the fate of an unsaved draft: one the user never really
// started (no name, no task, no event received) is dropped so abandoned setups
// don't litter the list; one with any work in it is kept AND persisted, so
// reopening shows what they typed rather than an empty shell.
async function onClosed() {
  stopPolling()
  const trig = current.value
  const draft = isUnsavedDraft.value
  const spec = draft && trig ? readRunSpec() : null
  const name = form.value.name
  const classify = { classify_enabled: form.value.classify_enabled, classifier_prompt: form.value.classifier_prompt, auth_mode: form.value.auth_mode }
  current.value = null
  sessionSecret.value = null
  isUnsavedDraft.value = false
  if (draft && trig) {
    const started = !!name.trim() || !!(spec?.task_template || '').trim() || !!trig.last_event
    try {
      if (started) {
        await useMyFetch(`/triggers/${trig.id}`, {
          method: 'PUT',
          body: { name, ...classify, ...spec, is_active: false },
        })
      } else {
        await useMyFetch(`/triggers/${trig.id}`, { method: 'DELETE' })
      }
    } catch { /* non-fatal: the draft stays in the list, inactive and deletable */ }
  }
  // Always: the modal is the only place a trigger changes, so whatever the
  // caller is listing is stale by the time it closes.
  emit('changed')
}

// Cancel returns to the summary when editing an existing trigger.
function onCancel() {
  if (!viewMode.value && current.value && !isUnsavedDraft.value) { viewMode.value = true; return }
  showModal.value = false
}

function readRunSpec() {
  const box = promptBoxRef.value as any
  const fallback = current.value || {}
  return {
    task_template: box?.getText?.() ?? fallback.task_template ?? '',
    mode: box?.getMode?.() || fallback.mode || 'chat',
    // Trust the box when it's mounted — its getModel() already maps Auto to
    // null. Use `??` (not `||`) so a deliberate Auto (null) is preserved
    // instead of falling back to the previously-saved model on edit.
    model_id: box ? (box.getModel?.() ?? null) : (fallback.model_id ?? null),
    // "" clears the binding server-side; the box returns null for "no project".
    project_id: box ? (box.getProject?.() ?? '') : (fallback.project_id ?? ''),
    data_source_ids: ((box?.getDataSources?.() as any[]) || fallback.data_sources || [])
      .map((d: any) => d?.id).filter(Boolean),
  }
}

// One save path — the Active toggle carries the on/off state, so there is no
// separate activate/pause action to reason about.
async function save() {
  if (!current.value || saving.value) return
  saving.value = true
  try {
    const body = { ...form.value, ...readRunSpec() }
    const { error } = await useMyFetch(`/triggers/${current.value.id}`, { method: 'PUT', body })
    if (error.value) throw error.value
    isUnsavedDraft.value = false
    toast.add({ title: t('triggers.toastSaved'), color: 'green' })
    emit('changed')
    showModal.value = false
  } catch (e) {
    console.error('save trigger failed', e)
    toast.add({ title: t('common.error'), description: t('triggers.saveFailed'), color: 'red' })
  } finally { saving.value = false }
}

// Pausing from the summary is a one-field write — no need to enter the form.
async function toggleActiveInPlace() {
  if (!current.value || saving.value) return
  const next = !form.value.is_active
  saving.value = true
  form.value.is_active = next
  try {
    const { error } = await useMyFetch(`/triggers/${current.value.id}`, {
      method: 'PUT', body: { is_active: next },
    })
    if (error.value) throw error.value
    isUnsavedDraft.value = false
    emit('changed')
  } catch {
    form.value.is_active = !next
    toast.add({ title: t('common.error'), description: t('triggers.saveFailed'), color: 'red' })
  } finally {
    saving.value = false
  }
}

// Rotate credentials. In secret-URL mode this also mints a new delivery URL
// (the URL is the credential there), which breaks the sender until it's
// re-pointed — hence the confirm.
async function rotate(trig: any) {
  if (!trig) return
  const urlMode = form.value.auth_mode === 'url_token'
  if (!confirm(t(urlMode ? 'triggers.rotateUrlConfirm' : 'triggers.rotateConfirm'))) return
  try {
    const { data, error } = await useMyFetch(`/triggers/${trig.id}/rotate`, { method: 'POST' })
    if (error.value || !data.value) throw error.value || new Error('rotate failed')
    const fresh = data.value as any
    sessionSecret.value = fresh.secret || null
    current.value = { ...current.value, ...fresh }
    toast.add({ title: t('triggers.toastRotated'), color: 'green' })
    emit('changed')
  } catch (e) {
    console.error('rotate trigger failed', e)
    toast.add({ title: t('common.error'), description: t('triggers.saveFailed'), color: 'red' })
  }
}

async function removeTrigger(trig: any) {
  if (!trig) return
  if (!confirm(t('triggers.deleteConfirm'))) return
  // Already gone — don't let the close handler try to clean it up again.
  isUnsavedDraft.value = false
  showModal.value = false
  await useMyFetch(`/triggers/${trig.id}`, { method: 'DELETE' })
  toast.add({ title: t('triggers.toastDeleted'), color: 'green' })
  emit('changed')
}

function copy(text: string, what: string = 'url') {
  if (!text) return
  navigator.clipboard.writeText(text)
  copied.value = what
  setTimeout(() => { if (copied.value === what) copied.value = null }, 1600)
}
</script>
