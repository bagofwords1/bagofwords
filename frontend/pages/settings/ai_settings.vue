<template>
    <div class="mt-6">
        <h2 class="text-lg font-medium text-gray-900 dark:text-white">{{ $t('settings.aiSettingsPage.title') }}
            <p class="text-sm text-gray-500 dark:text-gray-400 font-normal mb-8">
                {{ $t('settings.aiSettingsPage.subtitle') }}
            </p>
        </h2>

        <!-- Loading state -->
        <div v-if="loading" class="py-4">
            <ULoader />
        </div>

        <!-- Error message -->
        <UAlert v-if="error" class="mt-4" type="danger">
            {{ error }}
        </UAlert>

        <!-- AI Settings content: one collapsible card per section -->
        <div v-if="!loading && !error" class="space-y-3 md:w-2/3">
            <div
                v-for="section in visibleSections"
                :key="section.id"
                :data-testid="`ai-section-${section.id}`"
                class="border border-gray-200 dark:border-gray-800 rounded-lg"
            >
                <button
                    type="button"
                    class="w-full flex items-center justify-between gap-3 px-4 py-3 text-start"
                    :aria-expanded="isOpen(section.id)"
                    @click="toggleSection(section.id)"
                >
                    <div class="min-w-0">
                        <div class="font-medium text-gray-900 dark:text-white flex items-center">
                            {{ $t(`settings.aiSettingsPage.sections.${section.id}.title`) }}
                            <Icon v-if="section.hasLocked" name="heroicons:lock-closed" class="ms-2 w-4 h-4 text-gray-400 dark:text-gray-400" />
                        </div>
                        <div class="text-sm text-gray-500 dark:text-gray-400 truncate">
                            {{ $t(`settings.aiSettingsPage.sections.${section.id}.description`) }}
                        </div>
                    </div>
                    <div class="flex items-center gap-3 shrink-0">
                        <span v-if="!isOpen(section.id)" class="text-xs text-gray-500 dark:text-gray-400 hidden sm:inline">
                            {{ sectionSummary(section) }}
                        </span>
                        <Icon
                            name="heroicons:chevron-down"
                            :class="['w-5 h-5 text-gray-400 transition-transform', isOpen(section.id) ? 'rotate-180' : '']"
                        />
                    </div>
                </button>

                <div v-if="isOpen(section.id)" class="px-4 pb-5 pt-1 space-y-5 border-t border-gray-100 dark:border-gray-800">
                    <template v-for="key in section.keys" :key="key">
                        <!-- Allow LLM See Data - highlighted, confirmation-gated -->
                        <div v-if="key === 'allow_llm_see_data'" data-testid="ai-setting-allow_llm_see_data" class="flex flex-col mt-4 p-4 border-2 border-amber-300 bg-amber-50 dark:bg-amber-950 rounded-lg">
                            <div class="flex items-center justify-between">
                                <div class="font-medium flex items-center">
                                    <Icon name="heroicons:shield-exclamation" class="me-2 w-5 h-5 text-amber-600" />
                                    {{ featureLabel('allow_llm_see_data', configFeatures.allow_llm_see_data.name) }}
                                    <UTooltip v-if="configFeatures.allow_llm_see_data.state === 'locked'" :text="$t('settings.aiSettingsPage.locked')">
                                        <Icon name="heroicons:lock-closed" class="ms-2 w-4 h-4 text-gray-400 dark:text-gray-400" />
                                    </UTooltip>
                                </div>
                                <UToggle
                                    v-model="configFeatures.allow_llm_see_data.value"
                                    :disabled="!configFeatures.allow_llm_see_data.editable || configFeatures.allow_llm_see_data.state === 'locked'"
                                    @change="handleAllowLlmSeeDataChange"
                                />
                            </div>
                            <p class="text-sm text-amber-700 mt-2.5">{{ featureDescription('allow_llm_see_data', configFeatures.allow_llm_see_data.description) }}</p>
                            <p class="text-xs text-amber-600 mt-1 font-medium">
                                <Icon name="heroicons:exclamation-triangle" class="inline w-3 h-3 me-1" />
                                {{ $t('settings.aiSettingsPage.llmAccessWarning') }}
                            </p>
                        </div>
                        <AiSettingRow
                            v-else
                            :class="key === section.keys[0] ? 'mt-4' : ''"
                            :setting-key="key"
                            :feature="configFeatures[key]"
                            :label="featureLabel(key, configFeatures[key].name)"
                            :description="featureDescription(key, configFeatures[key].description)"
                            :child="!!FEATURE_PARENT[key]"
                            @change="updateConfigFeature(key, configFeatures[key])"
                        />
                    </template>
                </div>
            </div>

            <!-- No settings message -->
            <div v-if="visibleSections.length === 0" class="text-center py-8">
                <p class="text-gray-500 dark:text-gray-400">{{ $t('settings.aiSettingsPage.noSettings') }}</p>
            </div>
        </div>

        <!-- Confirmation Modal for Allow LLM See Data -->
        <UModal v-model="showLlmConfirmModal" :ui="{ width: 'sm:max-w-lg' }">
            <UCard :ui="{ body: { padding: 'p-6' }, header: { padding: 'px-6 py-4' }, footer: { padding: 'px-6 py-4' } }">
                <template #header>
                    <h3 class="text-lg font-semibold text-gray-900 dark:text-white">
                        {{ pendingLlmValue ? $t('settings.aiSettingsPage.llmModalTitleEnable') : $t('settings.aiSettingsPage.llmModalTitleDisable') }}
                    </h3>
                </template>

                <div class="space-y-4">
                    <!-- Enable message -->
                    <p v-if="pendingLlmValue" class="text-sm text-gray-600 dark:text-gray-400">
                        {{ $t('settings.aiSettingsPage.llmEnableMessage') }}
                    </p>

                    <!-- Disable message with impact list -->
                    <template v-else>
                        <p class="text-sm text-gray-600 dark:text-gray-400">
                            {{ $t('settings.aiSettingsPage.llmDisableIntro') }}
                        </p>
                        <ul class="text-sm text-gray-600 dark:text-gray-400 space-y-2 ms-1">
                            <li class="flex items-start gap-2">
                                <Icon name="heroicons:x-circle" class="w-4 h-4 text-red-500 mt-0.5 flex-shrink-0" />
                                <i18n-t keypath="settings.aiSettingsPage.llmImpactInspect" tag="span">
                                    <template #tool><strong>{{ $t('settings.aiSettingsPage.llmImpactInspectTool') }}</strong></template>
                                </i18n-t>
                            </li>
                            <li class="flex items-start gap-2">
                                <Icon name="heroicons:arrow-trending-down" class="w-4 h-4 text-amber-500 mt-0.5 flex-shrink-0" />
                                <span>{{ $t('settings.aiSettingsPage.llmImpactAccuracy') }}</span>
                            </li>
                            <li class="flex items-start gap-2">
                                <Icon name="heroicons:eye-slash" class="w-4 h-4 text-gray-400 dark:text-gray-400 mt-0.5 flex-shrink-0" />
                                <span>{{ $t('settings.aiSettingsPage.llmImpactColumns') }}</span>
                            </li>
                            <li class="mt-2 text-xs">
                            <UAlert :description="$t('settings.aiSettingsPage.llmFileUploadsNote')" class="text-xs" />
                        </li>
                        </ul>
                    </template>

                    <div class="pt-2">
                        <i18n-t keypath="settings.aiSettingsPage.llmConfirmLabel" tag="label" class="block text-sm text-gray-600 dark:text-gray-400 mb-2">
                            <template #phrase><span class="font-mono bg-gray-100 dark:bg-gray-800 px-1.5 py-0.5 rounded">{{ $t('settings.aiSettingsPage.llmConfirmPhrase') }}</span></template>
                        </i18n-t>
                        <UInput
                            v-model="llmConfirmText"
                            :placeholder="$t('settings.aiSettingsPage.llmConfirmPlaceholder')"
                            color="blue"
                            class="w-full"
                            @keyup.enter="confirmLlmChange"
                        />
                    </div>
                </div>

                <template #footer>
                    <div class="flex justify-end gap-3">
                        <UButton color="gray" variant="ghost" @click="cancelLlmChange">
                            {{ $t('settings.aiSettingsPage.cancel') }}
                        </UButton>
                        <UButton
                            :color="pendingLlmValue ? 'blue' : 'red'"
                            :disabled="llmConfirmText !== $t('settings.aiSettingsPage.llmConfirmPhrase')"
                            @click="confirmLlmChange"
                        >
                            {{ $t('settings.aiSettingsPage.confirm') }}
                        </UButton>
                    </div>
                </template>
            </UCard>
        </UModal>
    </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useToast } from '#imports'
import AiSettingRow from '~/components/settings/AiSettingRow.vue'

// Define feature interface matching backend FeatureConfig
interface Feature {
    name: string
    description: string
    value: any
    state: 'enabled' | 'disabled' | 'locked'
    editable: boolean
    is_lab: boolean
}

// Define response interface for better type safety
interface SettingsResponse {
    config?: {
        [key: string]: any
    }
}

definePageMeta({ auth: true, permissions: ['manage_settings'], layout: 'settings' })

const { t, te } = useI18n()

// Feature labels come from the backend as English literals (and are persisted
// into organization_settings.config on save, so a stored row can hold a stale
// English string). Translate off the stable setting KEY instead, and fall back
// to whatever the API sent when a locale has no entry yet — that keeps new
// backend settings readable instead of blank.
const featureLabel = (key: string, fallback?: string): string => {
    const path = `settings.aiSettingsPage.features.${key}.name`
    return te(path) ? t(path) : (fallback || key)
}
const featureDescription = (key: string, fallback?: string): string => {
    const path = `settings.aiSettingsPage.features.${key}.description`
    return te(path) ? t(path) : (fallback || '')
}
const loading = ref(true)
const error = ref('')

// Confirmation modal state
const showLlmConfirmModal = ref(false)
const llmConfirmText = ref('')
const pendingLlmValue = ref(false)

// Settings that only apply while a parent toggle is on. They are hidden while
// the parent is off and rendered right after it, indented, when it is on.
const FEATURE_PARENT: Record<string, string> = {
    enable_artifact_verification: 'allow_llm_see_data',
    ml_training_row_limit: 'enable_ml_training',
    checkins_max_per_user_per_week: 'enable_agent_checkins',
    checkins_max_runs_per_org_per_day: 'enable_agent_checkins',
    ai_suggestion_expiry_days: 'enable_agent_dreaming',
    mcp_native_tools_threshold: 'enable_mcp_native_tools',
    mcp_native_tools_max: 'enable_mcp_native_tools',
    max_webhooks: 'allow_report_webhooks',
    webhook_rate_limit_per_min: 'allow_report_webhooks',
}

// Page layout: settings grouped by what an admin is deciding, in display
// order. Keys are top-level FeatureConfig fields on OrganizationSettingsConfig.
// Dependents (FEATURE_PARENT) are placed automatically after their parent.
// A backend setting not listed here lands in "other" so it never disappears.
const SECTIONS: { id: string, keys: string[] }[] = [
    { id: 'data', keys: ['allow_llm_see_data', 'enable_web_fetch'] },
    { id: 'capabilities', keys: ['enable_training_mode', 'enable_agent_notes', 'enable_load_step', 'enable_ml_training', 'enable_custom_queries', 'enable_artifact_resources', 'enable_follow_ups'] },
    { id: 'learning', keys: ['enable_user_memory', 'suggest_instructions', 'enable_llm_judgement', 'auto_suggest_evals', 'enable_agent_dreaming', 'enable_agent_checkins'] },
    { id: 'tools', keys: ['enable_mcp_tools', 'enable_mcp_native_tools', 'mcp_result_inline_chars'] },
    { id: 'limits', keys: ['limit_row_count', 'mcp_create_data_preview_rows', 'agent_max_steps', 'agent_loop_retries', 'limit_code_retries', 'ai_tool_concurrency', 'query_timeout_seconds', 'max_concurrent_queries_per_connection', 'max_instructions_in_context', 'top_k_schema', 'top_k_metadata_resources', 'agent_roster_top_k'] },
    { id: 'workspace', keys: ['allow_report_webhooks', 'allow_forks', 'step_retention_days'] },
]

// Org settings that have a dedicated control elsewhere (LLM page: router and
// fallback, with their license gate; Integrations: MCP endpoint and Excel
// add-in). Not repeated here so there is one place to change each.
const MANAGED_ELSEWHERE = new Set(['model_routing', 'llm_fallback', 'mcp_enabled', 'enable_excel_addin'])

// Settings with no UI for now. enable_file_upload is not enforced yet (the
// upload routes and the attach button ignore it), so a switch here would
// promise a control it does not provide.
const HIDDEN = new Set(['enable_file_upload'])

const configFeatures = ref<Record<string, Feature>>({})

interface Section { id: string, keys: string[], total: number, on: number, booleans: number, hasLocked: boolean }

const visibleSections = computed<Section[]>(() => {
    const cfg = configFeatures.value
    const placed = new Set<string>([...MANAGED_ELSEWHERE, ...HIDDEN, ...Object.keys(FEATURE_PARENT)])
    const groups = SECTIONS.map(s => ({ id: s.id, keys: s.keys.filter(k => cfg[k]) }))
    for (const g of groups) g.keys.forEach(k => placed.add(k))
    const other = Object.keys(cfg).filter(k => !placed.has(k))
    if (other.length) groups.push({ id: 'other', keys: other })

    return groups.map(g => {
        const keys: string[] = []
        const all: string[] = []
        for (const key of g.keys) {
            keys.push(key)
            all.push(key)
            for (const child in FEATURE_PARENT) {
                if (FEATURE_PARENT[child] !== key || !cfg[child]) continue
                all.push(child)
                // Dependents follow their parent, only while the parent is on.
                if (cfg[key].value === true) keys.push(child)
            }
        }
        // The collapsed-header summary counts the section's own settings;
        // dependents only matter for the lock hint.
        const top = g.keys.map(k => cfg[k])
        const booleans = top.filter(f => typeof f.value === 'boolean')
        return {
            id: g.id,
            keys,
            total: top.length,
            on: booleans.filter(f => f.value === true).length,
            booleans: booleans.length,
            hasLocked: all.some(k => cfg[k].state === 'locked'),
        }
    }).filter(g => g.keys.length > 0)
})

const sectionSummary = (s: Section): string => s.booleans > 0
    ? t('settings.aiSettingsPage.sectionSummaryOn', { n: s.total, on: s.on })
    : t('settings.aiSettingsPage.sectionSummary', { n: s.total })

// Which sections are expanded. Data access starts open (it holds the risky
// toggle); the choice is a per-browser convenience, so storage is optional.
const OPEN_STORAGE_KEY = 'bow.aiSettings.openSections'
const openSections = ref<string[]>(['data'])
const isOpen = (id: string) => openSections.value.includes(id)
const toggleSection = (id: string) => {
    openSections.value = isOpen(id) ? openSections.value.filter(s => s !== id) : [...openSections.value, id]
    try { localStorage.setItem(OPEN_STORAGE_KEY, JSON.stringify(openSections.value)) } catch {}
}

const toast = useToast()

// Fetch organization settings
const fetchSettings = async () => {
    loading.value = true
    error.value = ''
    try {
        const response = await useMyFetch('/api/organization/settings')

        if (response.status.value !== 'success') {
            const errorData = response.error?.value?.data || { message: t('settings.aiSettingsPage.fetchError') }
            throw new Error(errorData.message || errorData.detail || t('settings.aiSettingsPage.fetchError'))
        }

        const data = response.data.value as SettingsResponse

        // Top-level FeatureConfig entries. ai_features is a legacy dict of
        // agent toggles with no UI.
        const allConfig = data.config || {}
        const generalConfig: Record<string, Feature> = {}

        for (const key in allConfig) {
            if (key !== 'ai_features' && typeof allConfig[key] === 'object' && allConfig[key]?.name) {
                generalConfig[key] = allConfig[key] as Feature
            }
        }
        configFeatures.value = generalConfig

    } catch (err: any) {
        error.value = err.message || t('settings.aiSettingsPage.fetchErrorGeneric')
        toast.add({
            title: t('settings.aiSettingsPage.toastFetchTitle'),
            description: error.value,
            color: 'red',
            timeout: 5000,
            icon: 'i-heroicons-exclamation-circle'
        })
    } finally {
        loading.value = false
    }
}

// Update general config feature setting
const updateConfigFeature = async (featureKey: string, feature: Feature) => {
    const originalValue = !feature.value
    try {
        const payload = {
            config: {
                [featureKey]: {
                    value: configFeatures.value[featureKey].value
                }
            }
        }

        const response = await useMyFetch('/api/organization/settings', {
            method: 'PUT',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(payload)
        })

        if (response.status.value !== 'success') {
            const errorData = response.error?.value?.data || { message: t('settings.aiSettingsPage.updateError') }
            throw new Error(errorData.message || errorData.detail || t('settings.aiSettingsPage.updateError'))
        }

        // Update the local state from response
        const updatedConfig = (response.data?.value as SettingsResponse)?.config
        if (updatedConfig?.[featureKey]) {
            configFeatures.value[featureKey] = updatedConfig[featureKey] as Feature
        } else {
            // Fallback: manually update state based on new value
            configFeatures.value[featureKey].state = configFeatures.value[featureKey].value ? 'enabled' : 'disabled'
        }

        toast.add({
            title: t('settings.aiSettingsPage.toastSuccessTitle'),
            description: t('settings.aiSettingsPage.toastSuccessBody', {
                name: featureLabel(featureKey, feature.name),
                state: feature.value ? t('settings.aiSettingsPage.stateEnabled') : t('settings.aiSettingsPage.stateDisabled')
            }),
            color: 'green',
            timeout: 3000
        })
    } catch (err: any) {
        // Revert the toggle
        configFeatures.value[featureKey].value = originalValue
        configFeatures.value[featureKey].state = originalValue ? 'enabled' : 'disabled'

        error.value = err.message || t('settings.aiSettingsPage.updateErrorGeneric')
        toast.add({
            title: t('settings.aiSettingsPage.toastUpdateTitle'),
            description: error.value,
            color: 'red',
            timeout: 5000,
            icon: 'i-heroicons-exclamation-circle'
        })
    }
}

// Handle allow_llm_see_data toggle - requires confirmation
const handleAllowLlmSeeDataChange = () => {
    // Store the new value and revert toggle until confirmed
    pendingLlmValue.value = configFeatures.value.allow_llm_see_data.value
    // Revert the toggle visually until confirmed
    configFeatures.value.allow_llm_see_data.value = !pendingLlmValue.value
    llmConfirmText.value = ''
    showLlmConfirmModal.value = true
}

// Confirm the allow_llm_see_data change
const confirmLlmChange = async () => {
    if (llmConfirmText.value !== t('settings.aiSettingsPage.llmConfirmPhrase')) {
        toast.add({
            title: t('settings.aiSettingsPage.toastConfirmRequiredTitle'),
            description: t('settings.aiSettingsPage.toastConfirmRequiredBody'),
            color: 'amber',
            timeout: 3000,
            icon: 'i-heroicons-exclamation-triangle'
        })
        return
    }

    // Apply the pending value
    configFeatures.value.allow_llm_see_data.value = pendingLlmValue.value
    showLlmConfirmModal.value = false
    llmConfirmText.value = ''

    // Now update the setting
    await updateConfigFeature('allow_llm_see_data', configFeatures.value.allow_llm_see_data)
}

// Cancel the allow_llm_see_data change
const cancelLlmChange = () => {
    showLlmConfirmModal.value = false
    llmConfirmText.value = ''
    // Toggle stays at original value (already reverted in handleAllowLlmSeeDataChange)
}

// Fetch settings when the component is mounted
onMounted(async () => {
    try {
        const saved = JSON.parse(localStorage.getItem(OPEN_STORAGE_KEY) || 'null')
        if (Array.isArray(saved)) openSections.value = saved
    } catch {}
    await fetchSettings()
})
</script>
