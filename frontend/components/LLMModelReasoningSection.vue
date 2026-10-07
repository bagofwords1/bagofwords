<template>
    <div class="py-2.5" data-testid="card-reasoning">
        <div class="flex items-center justify-between gap-3">
            <UTooltip :text="$t('settings.llms.reasoning.tooltip')">
                <span class="text-sm text-gray-700 dark:text-gray-300 underline decoration-dotted decoration-gray-300 underline-offset-2">{{ $t('settings.llms.reasoning.title') }}</span>
            </UTooltip>
            <USelect
                v-model="modeDraft"
                :options="modeOptions"
                size="xs"
                class="w-64"
                data-testid="card-reasoning-mode"
            />
        </div>

        <div v-if="modeDraft === 'like'" class="mt-2 flex items-center justify-between gap-3">
            <span class="text-xs text-gray-500 dark:text-gray-400">{{ $t('settings.llms.reasoning.likeLabel') }}</span>
            <input
                v-model="likeDraft"
                type="text"
                list="reasoning-like-models"
                :placeholder="$t('settings.llms.reasoning.likePlaceholder')"
                data-testid="card-reasoning-like"
                class="border border-gray-300 dark:border-gray-600 dark:bg-gray-800 rounded px-2 py-1 w-64 text-xs focus:outline-none focus:border-blue-500"
            />
            <datalist id="reasoning-like-models">
                <option v-for="id in likeSuggestions" :key="id" :value="id" />
            </datalist>
        </div>

        <!-- Default level for this model (used when the user leaves Default) -->
        <div class="mt-2 flex items-center justify-between gap-3">
            <span class="text-xs text-gray-500 dark:text-gray-400">{{ $t('settings.llms.reasoning.defaultLevel') }}</span>
            <div class="flex gap-0.5 p-0.5 rounded-md bg-gray-100 dark:bg-gray-800 w-64" :class="{ 'opacity-50': modeDraft === 'off' }">
                <button
                    v-for="opt in levelOptions"
                    :key="opt.value || 'default'"
                    type="button"
                    :disabled="modeDraft === 'off' && !!opt.value"
                    class="flex-auto px-1 py-0.5 rounded text-[11px] whitespace-nowrap transition-colors"
                    :class="defaultDraft === opt.value
                        ? 'bg-white dark:bg-gray-900 shadow-sm text-gray-900 dark:text-white font-medium'
                        : 'text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white'"
                    :data-testid="`card-reasoning-default-${opt.value || 'default'}`"
                    @click="defaultDraft = opt.value"
                >{{ opt.label }}</button>
            </div>
        </div>

        <!-- What each level runs as with the saved settings -->
        <div class="mt-2 text-[11px] text-gray-500 dark:text-gray-400" data-testid="card-reasoning-summary">
            <template v-if="reasoning?.supported">
                <span v-for="(lvl, i) in LEVELS" :key="lvl">
                    <span v-if="i" class="mx-1 text-gray-300">·</span>{{ $t(`prompt.effort.levels.${lvl}`) }} → <code class="text-gray-700 dark:text-gray-300">{{ reasoning.levels?.[lvl] || '—' }}</code>
                </span>
            </template>
            <span v-else>{{ $t('settings.llms.reasoning.unsupported') }}</span>
            <span v-if="isDirty" class="ms-1 italic">({{ $t('settings.llms.reasoning.savedSettings') }})</span>
        </div>

        <!-- Advanced: raw request fields per level -->
        <button
            type="button"
            class="mt-2 text-xs text-blue-600 dark:text-blue-400 hover:underline flex items-center gap-1"
            data-testid="card-reasoning-advanced"
            @click="showAdvanced = !showAdvanced"
        >
            <UIcon :name="showAdvanced ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" class="w-3.5 h-3.5 rtl:-scale-x-100" />
            {{ $t('settings.llms.reasoning.rawTitle') }}
            <span v-if="rawCount" class="text-gray-400">({{ rawCount }})</span>
        </button>
        <div v-if="showAdvanced" class="mt-2 space-y-2">
            <p class="text-[11px] text-gray-500 dark:text-gray-400">{{ $t('settings.llms.reasoning.rawHelp') }}</p>
            <div v-for="lvl in LEVELS" :key="lvl" class="space-y-1">
                <div class="flex items-center justify-between">
                    <span class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t(`prompt.effort.levels.${lvl}`) }}</span>
                    <div class="flex items-center gap-2">
                        <span v-if="testResults[lvl]" class="text-[11px]" :class="testResults[lvl].success ? 'text-green-600' : 'text-red-600'" :data-testid="`card-reasoning-result-${lvl}`">
                            <template v-if="testResults[lvl].success">
                                ✓ {{ $t('settings.llms.reasoning.ranAs', { level: testResults[lvl].runs_as || '—' }) }}
                                <template v-if="testResults[lvl].api"> · {{ testResults[lvl].api }}</template>
                                <template v-if="testResults[lvl].reasoning_tokens"> · {{ testResults[lvl].reasoning_tokens }} {{ $t('settings.llms.reasoning.tokens') }}</template>
                                · {{ ((testResults[lvl].latency_ms || 0) / 1000).toFixed(1) }}s
                            </template>
                            <template v-else>✗ <template v-if="testResults[lvl].api">{{ testResults[lvl].api }}: </template>{{ truncate(testResults[lvl].message) }}</template>
                        </span>
                        <UTooltip :text="isDirty ? $t('settings.llms.reasoning.saveToTest') : $t('settings.llms.reasoning.testTooltip')">
                            <UButton
                                size="2xs" color="gray" variant="soft"
                                :loading="testing === lvl"
                                :disabled="isDirty || !!testing"
                                :data-testid="`card-reasoning-test-${lvl}`"
                                @click="test(lvl)"
                            >{{ $t('settings.llms.reasoning.test') }}</UButton>
                        </UTooltip>
                    </div>
                </div>
                <textarea
                    v-model="rawDrafts[lvl]"
                    rows="2"
                    spellcheck="false"
                    :placeholder="rawPlaceholder"
                    :data-testid="`card-reasoning-raw-${lvl}`"
                    class="w-full font-mono text-[11px] border rounded px-2 py-1 dark:bg-gray-800 focus:outline-none"
                    :class="rawErrors[lvl] ? 'border-red-400 focus:border-red-500' : 'border-gray-300 dark:border-gray-600 focus:border-blue-500'"
                    dir="ltr"
                />
                <p v-if="rawErrors[lvl]" class="text-[11px] text-red-600">{{ rawErrors[lvl] }}</p>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
// Reasoning settings for one model: how effort is sent (mode), the model's
// default level, and raw request fields per level. Holds its own drafts; the
// card calls save() with the rest of its fields.
const props = defineProps<{ model: any }>()

const { t } = useI18n()
const LEVELS = ['low', 'medium', 'high', 'max'] as const

const reasoning = computed(() => props.model?.reasoning || null)
const modeDraft = ref<string>('auto')
const likeDraft = ref<string>('')
const defaultDraft = ref<string | null>(null)
const rawDrafts = reactive<Record<string, string>>({ low: '', medium: '', high: '', max: '' })
const showAdvanced = ref(false)
const testing = ref<string | null>(null)
const testResults = reactive<Record<string, any>>({})

const modeOptions = computed(() => [
    { value: 'auto', label: t('settings.llms.reasoning.modes.auto') },
    { value: 'like', label: t('settings.llms.reasoning.modes.like') },
    { value: 'generic', label: t('settings.llms.reasoning.modes.generic') },
    { value: 'custom', label: t('settings.llms.reasoning.modes.custom') },
    { value: 'off', label: t('settings.llms.reasoning.modes.off') },
])
const levelOptions = computed(() => [
    { value: null, label: t('prompt.effort.default') },
    ...LEVELS.map(l => ({ value: l, label: t(`prompt.effort.levels.${l}`) })),
])

// Suggestions for "behaves like": the catalog ids people usually deploy.
const likeSuggestions = [
    'gpt-6-astra', 'gpt-6.1-sol', 'gpt-6-sol', 'gpt-6-luna', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna',
    'gpt-5.5', 'gpt-5.4', 'gpt-5.4-mini', 'gpt-5.2', 'claude-sonnet-5-5', 'claude-sonnet-5', 'claude-opus-5-5',
    'claude-opus-4-8', 'claude-haiku-5-5', 'claude-haiku-4-5', 'gemini-3.6-flash', 'gemini-3.1-pro-preview',
]

const rawPlaceholder = computed(() => {
    const type = props.model?.provider?.provider_type
    const id = String(props.model?.model_id || '').toLowerCase()
    if (type === 'anthropic' || type === 'bedrock' || id.includes('claude')) {
        return '{"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}'
    }
    if (type === 'google' || id.includes('gemini')) return '{"thinking_config": {"thinking_budget": 8192}}'
    if (type === 'custom') return '{"reasoning_effort": "high"}  or  {"think": "high"}'
    // Azure and OpenAI-compatible gateways serve most models over Chat
    // Completions; GPT-5.6 / GPT-6 (and native OpenAI) use the Responses API.
    const responses = type === 'openai' && !props.model?.provider?.additional_config?.base_url
        || /^(gpt-6|gpt-5\.6)/.test(id)
    return responses
        ? '{"reasoning": {"summary": "detailed"}, "text": {"verbosity": "low"}}'
        : '{"verbosity": "low"}'
})

function syncDrafts() {
    const r = reasoning.value || {}
    modeDraft.value = r.mode || 'auto'
    likeDraft.value = r.like_model_id || ''
    defaultDraft.value = r.default || null
    for (const lvl of LEVELS) {
        const v = r.params?.[lvl]
        rawDrafts[lvl] = v ? JSON.stringify(v) : ''
        delete testResults[lvl]
    }
    showAdvanced.value = modeDraft.value === 'custom' || Object.keys(r.params || {}).length > 0
}
watch(() => props.model?.id, syncDrafts, { immediate: true })
watch(reasoning, syncDrafts)

// Custom mode needs the raw fields visible.
watch(modeDraft, (m) => { if (m === 'custom') showAdvanced.value = true })

const rawErrors = computed(() => {
    const out: Record<string, string> = {}
    for (const lvl of LEVELS) {
        const txt = rawDrafts[lvl].trim()
        if (!txt) continue
        try {
            const v = JSON.parse(txt)
            if (!v || typeof v !== 'object' || Array.isArray(v)) out[lvl] = t('settings.llms.reasoning.rawNotObject')
        } catch {
            out[lvl] = t('settings.llms.reasoning.rawInvalid')
        }
    }
    return out
})
const parsedParams = computed<Record<string, any> | null>(() => {
    const out: Record<string, any> = {}
    for (const lvl of LEVELS) {
        const txt = rawDrafts[lvl].trim()
        if (!txt || rawErrors.value[lvl]) continue
        out[lvl] = JSON.parse(txt)
    }
    return Object.keys(out).length ? out : null
})
const rawCount = computed(() => LEVELS.filter(l => rawDrafts[l].trim()).length)

const isDirty = computed(() => {
    const r = reasoning.value || {}
    return (r.mode || 'auto') !== modeDraft.value
        || (modeDraft.value === 'like' && (r.like_model_id || '') !== likeDraft.value.trim())
        || (r.default || null) !== defaultDraft.value
        || JSON.stringify(r.params && Object.keys(r.params).length ? r.params : null) !== JSON.stringify(parsedParams.value)
})

// Returns an error message, or null when saved (or nothing to save).
async function save(): Promise<string | null> {
    if (!isDirty.value) return null
    if (Object.keys(rawErrors.value).length) return t('settings.llms.reasoning.fixRaw')
    if (modeDraft.value === 'like' && !likeDraft.value.trim()) return t('settings.llms.reasoning.likeRequired')
    if (modeDraft.value === 'custom' && !parsedParams.value) return t('settings.llms.reasoning.customRequired')
    const response = await useMyFetch(`/llm/models/${props.model.id}/reasoning`, {
        method: 'POST',
        body: {
            mode: modeDraft.value,
            like_model_id: modeDraft.value === 'like' ? likeDraft.value.trim() : null,
            default_effort: defaultDraft.value,
            params: parsedParams.value,
        },
    })
    if (response.status.value !== 'success') {
        const err: any = (response.error as any)?.value || {}
        const detail = err?.data?.detail
        return typeof detail === 'string' ? detail : (Array.isArray(detail) ? detail[0]?.msg : t('settings.llms.reasoning.saveFailed'))
    }
    return null
}

async function test(lvl: string) {
    testing.value = lvl
    try {
        const { data, error } = await useMyFetch(`/llm/models/${props.model.id}/test_reasoning`, {
            method: 'POST',
            body: { effort: lvl },
        })
        testResults[lvl] = error.value
            ? { success: false, message: (error.value as any)?.data?.detail || 'Request failed' }
            : data.value
    } finally {
        testing.value = null
    }
}

function truncate(s: any) {
    const str = String(s || '')
    return str.length > 140 ? str.slice(0, 140) + '…' : str
}

defineExpose({ isDirty, save })
</script>
