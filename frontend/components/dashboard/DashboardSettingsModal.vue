<template>
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-lg' }">
        <div class="flex flex-col max-h-[85vh]" data-testid="dashboard-settings">
            <div class="flex items-start justify-between gap-3 px-6 pt-5 pb-2">
                <div class="min-w-0">
                    <h2 class="text-[15px] font-semibold text-gray-900 dark:text-white flex items-center gap-2">
                        {{ $t('dashboardSettings.title') }}
                        <!-- Everything here saves on its own; this says it did. -->
                        <span v-if="justSaved" class="inline-flex items-center gap-0.5 text-[11px] font-normal text-emerald-600 dark:text-emerald-400" data-testid="saved-indicator">
                            <Icon name="heroicons:check" class="w-3 h-3" />{{ $t('dashboardSettings.saved') }}
                        </span>
                    </h2>
                    <p class="text-xs text-gray-500 dark:text-gray-400 truncate">{{ dashboardTitle }}</p>
                </div>
                <button @click="isOpen = false" :aria-label="$t('common.close')"
                    class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 outline-none">
                    <Icon name="heroicons:x-mark" class="w-5 h-5" />
                </button>
            </div>

            <!-- One long page, top to bottom. Toggles save as they change; the
                 schedule has its own Save. -->
            <div class="flex-1 overflow-y-auto px-6 pb-2 divide-y divide-gray-100 dark:divide-gray-800">
                <!-- Saves on Enter or when the field loses focus. -->
                <form data-section="general" class="py-4 space-y-1.5" @submit.prevent="saveName">
                    <label for="dashboard-settings-name" class="text-xs font-medium text-gray-700 dark:text-gray-300 block">{{ $t('dashboardSettings.name') }}</label>
                    <div class="flex items-center gap-2">
                        <UInput id="dashboard-settings-name" v-model="nameDraft" class="flex-1" :maxlength="255"
                            :disabled="savingName" :placeholder="$t('artifactFrame.renamePlaceholder')" data-testid="settings-name"
                            @blur="saveName" />
                    </div>
                </form>

                <!-- Readable share-link name (/r/{slug}). Only while the
                     dashboard is shared: a private dashboard's link opens
                     for no one but its owner. -->
                <div v-if="isShared" data-section="link" class="py-4 space-y-1.5">
                    <span class="text-xs font-medium text-gray-700 dark:text-gray-300 block">{{ $t('dashboardSettings.link') }}</span>
                    <div v-if="!editingSlug" class="flex items-center gap-2">
                        <input :value="shareUrl" type="text" dir="ltr" data-testid="share-link" readonly
                            class="flex-1 h-8 px-2.5 border border-gray-200 dark:border-gray-700 rounded-md text-xs text-gray-600 dark:text-gray-400 bg-gray-50 dark:bg-gray-800 min-w-0" />
                        <UTooltip :text="$t('share.copyLink')">
                            <UButton color="gray" variant="ghost" :icon="copied ? 'i-heroicons-check' : 'i-heroicons-clipboard-document'"
                                :aria-label="$t('share.copyLink')" @click="copyLink" />
                        </UTooltip>
                        <UTooltip :text="$t('share.linkNameEdit')">
                            <UButton color="gray" variant="ghost" icon="i-heroicons-pencil-square" data-testid="slug-edit"
                                :aria-label="$t('share.linkNameEdit')" @click="startEditSlug" />
                        </UTooltip>
                    </div>
                    <div v-else>
                        <div class="flex items-center gap-2">
                            <div dir="ltr"
                                :class="['flex flex-1 items-center h-8 border rounded-md min-w-0 bg-white dark:bg-gray-900',
                                    slugError ? 'border-red-400' : 'border-gray-300 dark:border-gray-600 focus-within:border-blue-500']">
                                <span class="ps-2.5 text-xs text-gray-400 whitespace-nowrap truncate max-w-[55%]">{{ linkPrefix }}</span>
                                <input ref="slugInputRef" v-model="slugDraft" type="text" maxlength="80" data-testid="slug-input"
                                    :placeholder="$t('share.linkNamePlaceholder')"
                                    class="flex-1 min-w-0 h-full pe-2.5 text-xs text-gray-900 dark:text-white bg-transparent outline-none"
                                    @input="slugError = ''" @keydown.enter.prevent="saveSlug" @keydown.esc.stop.prevent="cancelEditSlug" />
                            </div>
                            <UButton color="blue" :loading="savingSlug" :disabled="!slugDraftValid" data-testid="slug-save" @click="saveSlug">
                                {{ $t('common.save') }}
                            </UButton>
                            <UButton color="gray" variant="ghost" :disabled="savingSlug" @click="cancelEditSlug">
                                {{ $t('common.cancel') }}
                            </UButton>
                        </div>
                        <p :class="['text-[11px] mt-1', slugError ? 'text-red-500' : 'text-gray-400']" data-testid="slug-hint">
                            {{ slugError || $t('share.linkNameHint', { min: SLUG_MIN, max: SLUG_MAX }) }}
                        </p>
                    </div>
                    <button v-if="linkSlug && !editingSlug" data-testid="slug-remove"
                        class="text-[11px] text-gray-500 hover:text-red-600 dark:text-gray-400" :disabled="savingSlug" @click="removeSlug">
                        {{ $t('share.linkNameRemove') }}
                    </button>
                </div>

                <!-- Schedule (stored on the report) -->
                <section data-section="schedule" class="py-4 space-y-3">
                    <h3 class="text-sm font-semibold text-gray-900 dark:text-white">{{ $t('dashboardSettings.schedule') }}</h3>
                    <ScheduleSettings :report="report" @saved="markSaved" />
                </section>

                <!-- Chat on the shared dashboard page (stored on the report).
                     Viewer threads are private per viewer and never touch this
                     report's own conversation. -->
                <section data-section="chat" class="py-4 space-y-3" data-testid="chat-settings">
                    <h3 class="text-sm font-semibold text-gray-900 dark:text-white">{{ $t('dashboardSettings.chat') }}</h3>
                    <div class="flex items-start justify-between gap-4">
                        <div class="flex flex-col min-w-0">
                            <span class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('share.allowChat') }}</span>
                            <span class="text-[11px] text-gray-500 dark:text-gray-400">{{ $t('share.allowChatDesc') }}</span>
                        </div>
                        <UToggle v-model="chatEnabled" :disabled="isSaving" class="flex-shrink-0 mt-0.5"
                            data-testid="chat-enabled-toggle" @update:model-value="onChatEnabledChange" />
                    </div>
                    <template v-if="chatEnabled">
                        <div class="space-y-2 pt-1">
                            <span class="text-xs font-medium text-gray-700 dark:text-gray-300 block">{{ $t('share.chatScope') }}</span>
                            <label class="flex items-start gap-2 cursor-pointer" data-testid="chat-scope-agents">
                                <input type="radio" value="agents" v-model="chatScope" :disabled="isSaving || reportAgents.length === 0"
                                    class="mt-0.5" @change="onChatScopeChange" />
                                <span class="flex flex-col">
                                    <span class="text-xs text-gray-700 dark:text-gray-300">{{ $t('share.chatScopeAgents') }}</span>
                                    <span class="text-[11px] text-gray-500 dark:text-gray-400">{{ $t('share.chatScopeAgentsDesc') }}</span>
                                </span>
                            </label>
                            <div v-if="chatScope === 'agents' && reportAgents.length > 0" class="ms-6 space-y-1">
                                <label v-for="agent in reportAgents" :key="agent.id" class="flex items-center gap-2 cursor-pointer">
                                    <input type="checkbox" :value="agent.id" v-model="chatAgentIds" :disabled="isSaving"
                                        @change="onChatAgentsChange" />
                                    <span class="text-xs text-gray-600 dark:text-gray-400">{{ agent.name }}</span>
                                </label>
                            </div>
                            <label class="flex items-start gap-2 cursor-pointer" data-testid="chat-scope-data-only">
                                <input type="radio" value="data_only" v-model="chatScope" :disabled="isSaving"
                                    class="mt-0.5" @change="onChatScopeChange" />
                                <span class="flex flex-col">
                                    <span class="text-xs text-gray-700 dark:text-gray-300">{{ $t('share.chatScopeDataOnly') }}</span>
                                    <span class="text-[11px] text-gray-500 dark:text-gray-400">{{ $t('share.chatScopeDataOnlyDesc') }}</span>
                                </span>
                            </label>
                            <p v-if="reportAgents.length === 0" class="text-[11px] text-gray-400">{{ $t('share.chatNoAgents') }}</p>
                        </div>

                        <!-- Default model for viewer chat: the report's own
                             model, the organization default, or a specific model. -->
                        <div class="space-y-1.5 pt-1" data-testid="chat-model">
                            <span class="text-xs font-medium text-gray-700 dark:text-gray-300 block">{{ $t('share.chatModel') }}</span>
                            <USelectMenu
                                v-model="chatModelValue"
                                :options="chatModelOptions"
                                value-attribute="value"
                                option-attribute="label"
                                size="sm"
                                :disabled="isSaving"
                                @change="onChatModelChange"
                            />
                            <span class="text-[11px] text-gray-500 dark:text-gray-400 block">{{ $t('share.chatModelDesc') }}</span>
                        </div>
                    </template>
                </section>

                <!-- What viewers of the shared dashboard get (stored on the report) -->
                <section data-section="viewers" class="py-4 space-y-3">
                    <h3 class="text-sm font-semibold text-gray-900 dark:text-white">{{ $t('dashboardSettings.viewers') }}</h3>
                    <div class="flex items-start justify-between gap-4">
                        <div class="flex flex-col min-w-0 flex-1">
                            <span class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('share.includeDataTab') }}</span>
                            <span class="text-[11px] text-gray-500 dark:text-gray-400">{{ $t('share.includeDataTabDesc') }}</span>
                        </div>
                        <UToggle v-model="includeDataTab" :disabled="isSaving" class="flex-shrink-0 mt-0.5"
                            @update:model-value="onIncludeDataTabChange" />
                    </div>
                    <!-- Whose credentials a viewer's "Run" uses. Only shown when
                         the choice exists — user-scoped sources (toggleable) or
                         RLS (visible but disabled, to explain why runs are
                         always per-viewer). -->
                    <div v-if="showRunIdentity" class="flex items-start justify-between gap-4">
                        <div class="flex flex-col min-w-0">
                            <span class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('share.runOnBehalf') }}</span>
                            <span class="text-[11px] text-gray-500 dark:text-gray-400">{{ hasRls ? $t('share.runOnBehalfRlsDisabled') : $t('share.runOnBehalfDesc') }}</span>
                        </div>
                        <UToggle v-model="runAsCreator" :disabled="isSaving || hasRls" class="flex-shrink-0 mt-0.5" @update:model-value="onRunIdentityChange" />
                    </div>
                </section>

            </div>

            <!-- Delete this dashboard (every version; the conversation and its
                 other dashboards stay). Pinned below the scroll so it is always
                 in view; the confirm dialog carries the explanation. -->
            <div data-section="delete" class="flex justify-start px-4 py-2.5 border-t border-gray-100 dark:border-gray-800">
                <UButton color="red" variant="ghost" size="sm" icon="i-heroicons-trash" :loading="deleting" data-testid="delete-dashboard" @click="deleteDashboard">
                    {{ $t('dashboardSettings.deleteButton') }}
                </UButton>
            </div>
        </div>
    </UModal>
</template>

<script lang="ts" setup>
import { ref, computed, watch, nextTick } from 'vue'
import ScheduleSettings from '~/components/dashboard/ScheduleSettings.vue'

const props = defineProps<{
    modelValue: boolean
    report: any
    // The dashboard (parent artifact id, stable across versions).
    artifactId: string
    dashboardTitle: string
    // Renames through ArtifactFrame, which keeps its list and the report title in step.
    rename: (title: string) => Promise<boolean>
}>()

const emit = defineEmits<{
    (e: 'update:modelValue', value: boolean): void
    (e: 'slug-changed', payload: { artifactId: string; slug: string | null }): void
    (e: 'deleted', payload: { artifactId: string }): void
}>()

const isOpen = computed({
    get: () => props.modelValue,
    set: (v: boolean) => emit('update:modelValue', v),
})

const toast = useToast()
const { t } = useI18n()
const { getErrorMessage } = useErrorMessage()
const isSaving = ref(false)

// "Saved ✓" next to the title for a moment after any successful save.
// Failures still toast.
const justSaved = ref(false)
let savedTimer: ReturnType<typeof setTimeout> | null = null
const markSaved = () => {
    justSaved.value = true
    if (savedTimer) clearTimeout(savedTimer)
    savedTimer = setTimeout(() => { justSaved.value = false }, 2000)
}

// ---- Name ----
const nameDraft = ref('')
const savingName = ref(false)
const nameChanged = computed(() => nameDraft.value.trim() !== (props.dashboardTitle || ''))

const saveName = async () => {
    const title = nameDraft.value.trim()
    if (!title || !nameChanged.value || savingName.value) return
    savingName.value = true
    try {
        if (await props.rename(title)) markSaved()
        else nameDraft.value = props.dashboardTitle || ''
    } finally {
        savingName.value = false
    }
}

// ---- Link name (/r/{slug}) ----
// Shown while the dashboard is shared. The format check mirrors the server's
// (which also rejects reserved and UUID-shaped names) so Save is only offered
// for a plausible name.
const SLUG_MIN = 3
const SLUG_MAX = 80
const SLUG_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/
const isShared = ref(false)
const linkSlug = ref<string | null>(null)
const editingSlug = ref(false)
const slugDraft = ref('')
const slugError = ref('')
const savingSlug = ref(false)
const slugInputRef = ref<HTMLInputElement | null>(null)
const copied = ref(false)
const linkPrefix = computed(() => `${window.location.origin}/r/`)
const normalizedSlugDraft = computed(() => slugDraft.value.trim().toLowerCase())
const slugDraftValid = computed(() => {
    const v = normalizedSlugDraft.value
    return v.length >= SLUG_MIN && v.length <= SLUG_MAX && SLUG_RE.test(v)
})

const shareUrl = computed(() => (linkSlug.value
    ? `${linkPrefix.value}${linkSlug.value}`
    : `${window.location.origin}/r/${props.report.id}?artifact=${encodeURIComponent(props.artifactId)}`))

// This dashboard's own sharing state: whether it is shared, and its name.
const fetchSharing = async () => {
    const artifactId = props.artifactId
    try {
        const res = await useMyFetch(`/reports/${props.report.id}/artifacts/${artifactId}/sharing`)
        if (res.error.value || !res.data.value || artifactId !== props.artifactId) return
        const data = res.data.value as any
        isShared.value = (data.visibility || 'none') !== 'none'
        linkSlug.value = data.slug ?? null
    } catch { /* silent */ }
}

const startEditSlug = () => {
    slugDraft.value = linkSlug.value || ''
    slugError.value = ''
    editingSlug.value = true
    nextTick(() => slugInputRef.value?.focus())
}

const cancelEditSlug = () => {
    editingSlug.value = false
    slugError.value = ''
}

const putSlug = async (slug: string | null): Promise<boolean> => {
    const artifactId = props.artifactId
    savingSlug.value = true
    try {
        const res = await useMyFetch(`/reports/${props.report.id}/artifacts/${artifactId}/slug`, {
            method: 'PUT',
            body: { slug },
        })
        if (res.error.value) {
            slugError.value = getErrorMessage(res.error.value, t('share.linkNameFailed'))
            return false
        }
        if (artifactId !== props.artifactId) return false
        linkSlug.value = (res.data.value as any)?.slug ?? null
        emit('slug-changed', { artifactId, slug: linkSlug.value })
        return true
    } finally {
        savingSlug.value = false
    }
}

const saveSlug = async () => {
    if (!slugDraftValid.value || savingSlug.value) return
    if (normalizedSlugDraft.value === linkSlug.value) {
        editingSlug.value = false
        return
    }
    if (await putSlug(normalizedSlugDraft.value)) {
        editingSlug.value = false
        markSaved()
    }
}

// Removing frees the name and every earlier one: links already sent with
// them stop working, so ask first.
const removeSlug = async () => {
    if (!window.confirm(t('share.linkNameRemoveConfirm'))) return
    if (await putSlug(null)) {
        markSaved()
    } else if (slugError.value) {
        toast.add({ title: slugError.value, color: 'red' })
        slugError.value = ''
    }
}

const copyLink = async () => {
    try {
        await navigator.clipboard.writeText(shareUrl.value)
        copied.value = true
        setTimeout(() => { copied.value = false }, 2000)
    } catch {
        toast.add({ title: t('share.copyFailed'), color: 'red' })
    }
}

// ---- Viewer settings (report-level: Data tab, run identity, chat) ----
const includeDataTab = ref(true)
// Whose credentials a shared-dashboard viewer's "Run" uses:
// off = the viewer's own ('viewer'), on = on behalf of the owner ('creator')
const runAsCreator = ref(false)
// RLS dashboards force per-viewer identity — creator mode is disabled.
const hasRls = ref(false)
// Only user-scoped (user_required) sources make the run-identity toggle
// meaningful; on system-only credentials both identities resolve to the same
// credentials, so the control is hidden.
const hasUserScoped = ref(false)
const showRunIdentity = computed(() => hasUserScoped.value || hasRls.value)

// Chat scope maps onto the backend's artifact_chat_data_source_ids: 'agents'
// with everything checked = null (inherit the roster, sent as the ["*"] reset
// sentinel), a subset = that list, 'data_only' = [].
const chatEnabled = ref(false)
const chatScope = ref<'agents' | 'data_only'>('agents')
const chatAgentIds = ref<string[]>([])
const reportAgents = ref<{ id: string; name: string }[]>([])
// Default model for dashboard chat. Stored values: null = inherit the
// report's own model, ORG_DEFAULT_MODEL = the organization default, else a
// model id. USelectMenu treats '' as "nothing selected" (blank label), so
// inherit carries its own sentinel and is sent as the backend's "" clear value.
const INHERIT_MODEL = '__inherit__'
const ORG_DEFAULT_MODEL = 'org_default'
const chatModelId = ref(INHERIT_MODEL)
const chatModels = ref<{ id: string; name: string; provider?: string; isDefault?: boolean }[]>([])
// The report's own model override (what INHERIT_MODEL resolves to).
const reportModelId = ref('')
const modelLabel = (m: { name: string; provider?: string }) => (m.provider ? `${m.name} · ${m.provider}` : m.name)
// Without a report model, inheriting IS the organization default: show one
// option for both stored values instead of two identical ones.
const chatModelValue = computed({
    get: () => (!reportModelId.value && chatModelId.value === INHERIT_MODEL ? ORG_DEFAULT_MODEL : chatModelId.value),
    set: (v: string) => { chatModelId.value = v },
})
const chatModelOptions = computed(() => {
    const orgDefault = chatModels.value.find(m => m.isDefault)
    const orgOption = {
        value: ORG_DEFAULT_MODEL,
        label: orgDefault ? t('share.chatModelOrgDefault', { name: orgDefault.name }) : t('share.chatModelOrgDefaultPlain'),
    }
    const options = reportModelId.value
        ? (() => {
            const inherited = chatModels.value.find(m => m.id === reportModelId.value)
            return [
                { value: INHERIT_MODEL, label: inherited ? t('share.chatModelReport', { name: inherited.name }) : t('share.chatModelReportPlain') },
                orgOption,
            ]
        })()
        : [orgOption]
    options.push(...chatModels.value.map(m => ({ value: m.id, label: modelLabel(m) })))
    // A stored pick that was since disabled/deleted: keep it visible (chat
    // falls back to the default at run time) instead of a blank select.
    if (![INHERIT_MODEL, ORG_DEFAULT_MODEL].includes(chatModelId.value) && !chatModels.value.some(m => m.id === chatModelId.value)) {
        options.push({ value: chatModelId.value, label: t('share.chatModelUnavailable') })
    }
    return options
})

const fetchReportSettings = async () => {
    try {
        const res = await useMyFetch(`/reports/${props.report.id}`, { method: 'GET' })
        if (!res.data.value) return
        const data = res.data.value as any
        hasRls.value = !!data.has_rls
        hasUserScoped.value = !!data.has_user_scoped
        if (data.shared_run_identity !== undefined) {
            runAsCreator.value = data.shared_run_identity === 'creator'
            props.report.shared_run_identity = data.shared_run_identity
        }
        if (data.include_data_tab !== undefined) {
            includeDataTab.value = data.include_data_tab !== false
            props.report.include_data_tab = data.include_data_tab
        }
        if (data.artifact_chat_enabled !== undefined) {
            chatEnabled.value = data.artifact_chat_enabled === true
            props.report.artifact_chat_enabled = data.artifact_chat_enabled
        }
        // Candidate agents = what the report actually uses (attached roster,
        // or recovered from its runs for Auto reports) — not the raw
        // attachment list, which is empty under Auto.
        try {
            const agentsRes = await useMyFetch(`/reports/${props.report.id}/artifact_chat/agents`)
            const payload = agentsRes.data.value as any
            reportAgents.value = (payload?.agents || []).map((a: any) => ({ id: a.id, name: a.name }))
        } catch {
            reportAgents.value = (data.data_sources || []).map((ds: any) => ({ id: ds.id, name: ds.name }))
        }
        chatModelId.value = data.artifact_chat_model_id || INHERIT_MODEL
        props.report.artifact_chat_model_id = data.artifact_chat_model_id || null
        reportModelId.value = data.model_id || ''
        try {
            const modelsRes = await useMyFetch('/llm/models?is_enabled=true')
            chatModels.value = ((modelsRes.data.value as any[]) || []).map((m: any) => ({
                id: m.id, name: m.name || m.model_id, provider: m.provider?.name, isDefault: !!m.is_default,
            }))
        } catch { chatModels.value = [] }
        const storedIds = data.artifact_chat_data_source_ids
        if (storedIds === null || storedIds === undefined) {
            chatScope.value = reportAgents.value.length > 0 ? 'agents' : 'data_only'
            chatAgentIds.value = reportAgents.value.map(a => a.id)
        } else if (Array.isArray(storedIds) && storedIds.length === 0) {
            chatScope.value = 'data_only'
            chatAgentIds.value = reportAgents.value.map(a => a.id)
        } else {
            chatScope.value = 'agents'
            chatAgentIds.value = storedIds
        }
    } catch { /* silent */ }
}

// One PUT per change. No visibility in the body: the endpoint then leaves
// every grant as is and writes only the field being changed.
const saveReportSetting = async (body: Record<string, any>, revert: () => void) => {
    isSaving.value = true
    try {
        const res = await useMyFetch(`/reports/${props.report.id}/visibility/artifact`, {
            method: 'PUT',
            body,
        })
        if (res.error.value) throw res.error.value
        markSaved()
    } catch {
        revert()
        toast.add({ title: t('share.sharingFailed'), color: 'red' })
    } finally {
        isSaving.value = false
    }
}

const onRunIdentityChange = async (value: boolean) => {
    const identity = value ? 'creator' : 'viewer'
    await saveReportSetting({ run_identity: identity }, () => { runAsCreator.value = !value })
    if (runAsCreator.value === value) props.report.shared_run_identity = identity
}

const onIncludeDataTabChange = async (value: boolean) => {
    // Put the box back where it was if the setting did not change.
    await saveReportSetting({ include_data_tab: value }, () => { includeDataTab.value = !value })
    if (includeDataTab.value === value) props.report.include_data_tab = value
}

const chatScopeIdsPayload = (): string[] => {
    if (chatScope.value === 'data_only') return []
    const all = reportAgents.value.map(a => a.id)
    const picked = chatAgentIds.value.filter(id => all.includes(id))
    // Everything checked = inherit the roster (["*"] resets to null server-side).
    if (picked.length === all.length && all.length > 0) return ['*']
    return picked
}

const onChatEnabledChange = async (value: boolean) => {
    props.report.artifact_chat_enabled = value
    await saveReportSetting(
        { artifact_chat_enabled: value },
        () => { chatEnabled.value = !value; props.report.artifact_chat_enabled = !value },
    )
}

const onChatScopeChange = async () => {
    if (chatScope.value === 'agents' && chatAgentIds.value.length === 0) {
        chatAgentIds.value = reportAgents.value.map(a => a.id)
    }
    await saveReportSetting({ artifact_chat_data_source_ids: chatScopeIdsPayload() }, () => {})
}

const onChatAgentsChange = async () => {
    await saveReportSetting({ artifact_chat_data_source_ids: chatScopeIdsPayload() }, () => {})
}

const onChatModelChange = async (value: string) => {
    const next = value === INHERIT_MODEL ? '' : value
    const prev = props.report.artifact_chat_model_id || ''
    if (next === prev) return
    props.report.artifact_chat_model_id = next || null
    await saveReportSetting(
        { artifact_chat_model_id: next },
        () => { chatModelId.value = prev || INHERIT_MODEL; props.report.artifact_chat_model_id = prev || null },
    )
}

// ---- Delete ----
const deleting = ref(false)
const deleteDashboard = async () => {
    if (!window.confirm(t('dashboardSettings.deleteConfirm', { name: props.dashboardTitle }))) return
    const artifactId = props.artifactId
    deleting.value = true
    try {
        const res = await useMyFetch(`/reports/${props.report.id}/artifacts/${artifactId}`, { method: 'DELETE' })
        if (res.error.value) throw res.error.value
        toast.add({ title: t('dashboardSettings.deleted'), color: 'green' })
        isOpen.value = false
        emit('deleted', { artifactId })
    } catch (e: any) {
        toast.add({ title: getErrorMessage(e, t('dashboardSettings.deleteFailed')), color: 'red' })
    } finally {
        deleting.value = false
    }
}

// ---- Open ----
watch(() => props.modelValue, async (open) => {
    if (!open) return
    nameDraft.value = props.dashboardTitle || ''
    editingSlug.value = false
    includeDataTab.value = props.report?.include_data_tab !== false
    runAsCreator.value = props.report?.shared_run_identity === 'creator'
    chatEnabled.value = props.report?.artifact_chat_enabled === true
    await Promise.all([fetchSharing(), fetchReportSettings()])
}, { immediate: true })
</script>
