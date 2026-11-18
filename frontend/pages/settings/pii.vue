<template>
    <div>
        <!-- Enterprise gate (belt-and-suspenders; the tab is already hidden when unlicensed) -->
        <div v-if="!hasFeature('pii_protection')" class="rounded-lg border border-amber-200 bg-amber-50/70 dark:bg-amber-950/40 dark:border-amber-900/60 p-5">
            <div class="flex items-center gap-2 text-sm font-semibold text-amber-800 dark:text-amber-300">
                <Icon name="heroicons:sparkles" class="w-4 h-4" />
                {{ $t('settings.pii.enterpriseTitle') }}
            </div>
            <p class="text-sm text-amber-800/90 dark:text-amber-300/90 mt-1.5">{{ $t('settings.pii.enterpriseBody') }}</p>
        </div>

        <template v-else>
            <SettingsLoading v-if="loading" />

            <div v-else>
                <!-- Master switch -->
                <SettingsSection>
                    <SettingsRow :label="$t('settings.pii.masterLabel')" :description="$t('settings.pii.masterDesc')">
                        <UToggle v-model="pii.enabled" @update:model-value="save" />
                    </SettingsRow>
                </SettingsSection>

                <!-- Which policy each LLM provider uses -->
                <SettingsSection :title="$t('settings.pii.providersTitle')" :description="$t('settings.pii.providersDesc')">
                    <div data-testid="pii-provider-policies" :class="{ 'opacity-50 pointer-events-none': !pii.enabled }">
                        <div v-if="providers.length === 0" class="rounded-lg border border-dashed border-line">
                            <SettingsEmpty icon="i-heroicons-cpu-chip" :title="$t('settings.pii.noProviders')" compact />
                        </div>
                        <div v-else class="bow-table-card divide-y divide-line-subtle">
                            <div v-for="provider in providers" :key="provider.id" data-testid="pii-provider-row"
                                 class="flex items-center justify-between gap-4 px-5 py-3">
                                <div class="flex items-center gap-3 min-w-0">
                                    <LLMProviderIcon :provider="provider.provider_type" :icon="true" class="w-5 h-5 shrink-0" />
                                    <div class="min-w-0">
                                        <div class="text-sm font-medium text-ink truncate">{{ provider.name }}</div>
                                        <div class="text-xs text-ink-muted">{{ providerSummary(provider.id) }}</div>
                                    </div>
                                </div>
                                <USelect
                                    :model-value="providerPolicy(provider.id)"
                                    :options="policyOptions"
                                    :aria-label="$t('settings.pii.providerPolicyFor', { name: provider.name })"
                                    data-testid="pii-provider-policy"
                                    size="sm" class="w-64 shrink-0"
                                    @update:model-value="setProviderPolicy(provider.id, $event); save()" />
                            </div>
                        </div>
                    </div>
                </SettingsSection>

                <!-- Policies: pick one, then edit its options below -->
                <SettingsSection :title="$t('settings.pii.policiesTitle')" :description="$t('settings.pii.policiesDesc')">
                    <template #actions>
                        <UButton size="sm" color="gray" icon="i-heroicons-plus" :disabled="!pii.enabled"
                                 data-testid="pii-new-policy" @click="addPolicy">{{ $t('settings.pii.newPolicy') }}</UButton>
                    </template>

                    <div :class="{ 'opacity-50 pointer-events-none': !pii.enabled }">
                        <div class="flex flex-wrap gap-2" role="group" :aria-label="$t('settings.pii.policiesTitle')" data-testid="pii-policy-picker">
                            <UButton v-for="(policy, id) in pii.policies" :key="id" size="sm"
                                     :color="id === selected ? 'primary' : 'gray'"
                                     :variant="id === selected ? 'soft' : 'ghost'"
                                     :aria-pressed="id === selected"
                                     :data-testid="`pii-policy-${id}`"
                                     @click="selected = String(id)">
                                {{ policyLabel(String(id)) }}
                                <span class="text-2xs text-ink-muted nums">{{ $t('settings.pii.providerCount', { n: providerCount(String(id)) }, providerCount(String(id))) }}</span>
                            </UButton>
                        </div>

                        <div class="mt-5 rounded-lg border border-line p-4 space-y-5" data-testid="pii-policy-editor">
                            <div v-if="selected !== 'default'" class="flex items-end gap-3">
                                <div class="flex-1">
                                    <label class="block mb-1 text-xs font-medium text-ink-muted" for="pii-policy-name">{{ $t('settings.pii.policyName') }}</label>
                                    <UInput id="pii-policy-name" v-model="current.name" size="sm" maxlength="60"
                                            data-testid="pii-policy-name" @blur="save" />
                                </div>
                                <UButton color="red" variant="ghost" size="sm" icon="i-heroicons-trash"
                                         data-testid="pii-delete-policy" @click="removePolicy">{{ $t('settings.pii.deletePolicy') }}</UButton>
                            </div>
                            <p v-else class="text-xs text-ink-muted">{{ $t('settings.pii.defaultPolicyNote') }}</p>

                            <fieldset>
                                <legend class="text-sm font-medium text-ink mb-3">{{ $t('settings.pii.modeLabel') }}</legend>
                                <div class="grid gap-3 sm:grid-cols-2">
                                    <label
                                        v-for="opt in modeOptions"
                                        :key="opt.value"
                                        :class="[
                                            'flex items-start gap-3 rounded-lg border px-4 py-3 cursor-pointer transition-colors',
                                            current.mode === opt.value
                                                ? 'border-blue-500 bg-blue-50/60 dark:border-blue-400 dark:bg-blue-950/30'
                                                : 'border-line hover:border-line-strong'
                                        ]"
                                    >
                                        <input type="radio" :value="opt.value" v-model="current.mode" class="mt-0.5 accent-blue-600" @change="save" />
                                        <span class="min-w-0">
                                            <span class="block text-sm font-medium text-ink">{{ opt.label }}</span>
                                            <span class="block mt-0.5 text-xs text-ink-muted leading-relaxed">{{ opt.description }}</span>
                                        </span>
                                    </label>
                                </div>
                            </fieldset>

                            <SettingsRow :label="$t('settings.pii.redactContextLabel')" :description="$t('settings.pii.redactContextDesc')">
                                <UToggle v-model="current.redact_context" data-testid="pii-redact-context" @update:model-value="save" />
                            </SettingsRow>
                        </div>
                    </div>
                </SettingsSection>

                <!-- Built-in rules (options of the selected policy) -->
                <SettingsSection :title="$t('settings.pii.builtinTitle')" :description="$t('settings.pii.builtinDescPolicy', { policy: policyLabel(selected) })">
                    <div :class="['bow-table-card divide-y divide-line-subtle', { 'opacity-50 pointer-events-none': !pii.enabled }]">
                        <div v-for="rule in builtinRules" :key="rule.id" class="flex items-center justify-between gap-4 px-5 py-3">
                            <div class="min-w-0">
                                <div class="text-sm font-medium text-ink">{{ rule.name }}</div>
                                <div class="text-xs text-ink-muted">{{ $t('settings.pii.patternCount', { n: rule.pattern_count }, rule.pattern_count) }}</div>
                            </div>
                            <div class="flex items-center gap-3 shrink-0">
                                <USelect
                                    :model-value="ruleAction('builtin', rule.id)"
                                    @update:model-value="setRuleOption('builtin', rule.id, 'action', $event); save()"
                                    :options="actionOptions" size="sm" class="w-52" />
                                <UInput
                                    v-if="ruleAction('builtin', rule.id) === 'replace'"
                                    :model-value="ruleReplacement('builtin', rule.id, rule.replacement)"
                                    @update:model-value="setRuleOption('builtin', rule.id, 'replacement', $event)"
                                    @blur="save"
                                    size="sm" class="w-44" input-class="font-mono text-xs" :placeholder="rule.replacement" />
                                <PiiActionHint v-else :action="ruleAction('builtin', rule.id)" class="w-44 justify-end" />
                                <UToggle
                                    :model-value="ruleEnabled('builtin', rule.id)"
                                    @update:model-value="setRuleOption('builtin', rule.id, 'enabled', $event); save()" />
                            </div>
                        </div>
                    </div>
                </SettingsSection>

                <!-- Names, locations, organizations: found by a detector (Presidio), not a pattern -->
                <SettingsSection :title="$t('settings.pii.detectorTitle')"
                                 :description="detector.available ? $t('settings.pii.detectorDescPolicy', { policy: policyLabel(selected) }) : $t('settings.pii.detectorUnavailable')">
                    <div v-if="detector.available" data-testid="pii-detector-rules"
                         :class="{ 'opacity-50 pointer-events-none': !pii.enabled }">
                        <div class="bow-table-card divide-y divide-line-subtle">
                            <div v-for="rule in detector.entities" :key="rule.id" data-testid="pii-detector-row"
                                 class="flex items-center justify-between gap-4 px-5 py-3">
                                <div class="min-w-0">
                                    <div class="text-sm font-medium text-ink">{{ $t(`settings.pii.entities.${rule.id}`) }}</div>
                                    <div class="text-xs text-ink-muted">{{ $t('settings.pii.detectedByModel') }}</div>
                                </div>
                                <div class="flex items-center gap-3 shrink-0">
                                    <label class="flex items-center gap-1.5 text-xs text-ink-muted">
                                        {{ $t('settings.pii.minScore') }}
                                        <UInput type="number" min="0" max="1" step="0.05" size="sm" class="w-20" input-class="nums"
                                                :model-value="detectorOption(rule.id).min_score ?? defaultMinScore"
                                                :disabled="!detectorEnabled(rule.id)"
                                                :aria-label="$t('settings.pii.minScore')"
                                                @update:model-value="setDetectorOption(rule.id, 'min_score', clampScore($event))"
                                                @blur="save" />
                                    </label>
                                    <USelect
                                        :model-value="detectorOption(rule.id).action || current.mode"
                                        :disabled="!detectorEnabled(rule.id)"
                                        @update:model-value="setDetectorOption(rule.id, 'action', $event); save()"
                                        :options="actionOptions" size="sm" class="w-52" />
                                    <UToggle
                                        :model-value="detectorEnabled(rule.id)"
                                        :aria-label="$t(`settings.pii.entities.${rule.id}`)"
                                        @update:model-value="setDetectorOption(rule.id, 'enabled', $event); save()" />
                                </div>
                            </div>
                        </div>

                        <div class="mt-4">
                            <SettingsRow :label="$t('settings.pii.detectorErrorLabel')" :description="$t('settings.pii.detectorErrorDesc')">
                                <USelect v-model="current.on_detector_error" :options="detectorErrorOptions" size="sm" class="w-64"
                                         data-testid="pii-detector-error" @update:model-value="save" />
                            </SettingsRow>
                        </div>
                    </div>
                </SettingsSection>

                <!-- Custom rules: patterns shared, options per policy -->
                <SettingsSection :title="$t('settings.pii.customTitle')" :description="$t('settings.pii.customDescShared')">
                    <template #actions>
                        <UButton size="sm" color="gray" icon="i-heroicons-plus" :disabled="!pii.enabled" @click="addRule">{{ $t('settings.pii.addRule') }}</UButton>
                    </template>

                    <div :class="{ 'opacity-50 pointer-events-none': !pii.enabled }">
                        <div v-if="pii.custom_rules.length === 0" class="rounded-lg border border-dashed border-line">
                            <SettingsEmpty icon="i-heroicons-funnel" :title="$t('settings.pii.noCustomRules')" compact />
                        </div>

                        <div v-for="(rule, ri) in pii.custom_rules" :key="rule.id"
                             class="rounded-lg border border-line p-4 space-y-4 [&+&]:mt-3">
                            <div class="flex items-center justify-between gap-3">
                                <div class="flex items-center gap-3 flex-1">
                                    <UToggle :model-value="ruleEnabled('custom', rule.id)"
                                             @update:model-value="setRuleOption('custom', rule.id, 'enabled', $event); save()" />
                                    <UInput v-model="rule.name" :placeholder="$t('settings.pii.ruleNamePlaceholder')"
                                            size="sm" class="flex-1" @blur="save" />
                                </div>
                                <UButton color="red" variant="ghost" size="sm" icon="i-heroicons-trash"
                                         @click="removeRule(ri)">{{ $t('settings.pii.removeRule') }}</UButton>
                            </div>

                            <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                                <div>
                                    <label class="block mb-1 text-xs font-medium text-ink-muted">{{ $t('settings.pii.actionInPolicy', { policy: policyLabel(selected) }) }}</label>
                                    <USelect
                                        :model-value="ruleAction('custom', rule.id)"
                                        @update:model-value="setRuleOption('custom', rule.id, 'action', $event); save()"
                                        :options="actionOptions" size="sm" />
                                </div>
                                <div v-if="ruleAction('custom', rule.id) === 'replace'">
                                    <label class="block mb-1 text-xs font-medium text-ink-muted">{{ $t('settings.pii.replacement') }}</label>
                                    <UInput :model-value="ruleReplacement('custom', rule.id, rule.replacement)"
                                            @update:model-value="setRuleOption('custom', rule.id, 'replacement', $event)"
                                            size="sm" input-class="font-mono text-xs" :placeholder="rule.replacement" @blur="save" />
                                </div>
                                <div v-else class="flex items-end">
                                    <PiiActionHint :action="ruleAction('custom', rule.id)" class="pb-2" />
                                </div>
                            </div>

                            <div>
                                <label class="block text-xs font-medium text-ink-muted">{{ $t('settings.pii.patternsShared') }}</label>
                                <div v-for="(pat, pi) in rule.patterns" :key="pi" class="flex items-center gap-2 mt-1.5">
                                    <UInput v-model="rule.patterns[pi]" size="sm"
                                            class="flex-1" input-class="font-mono text-xs" :placeholder="$t('settings.pii.patternPlaceholder')" @blur="save" />
                                    <UButton color="gray" variant="ghost" size="xs" icon="i-heroicons-x-mark"
                                             :aria-label="$t('common.remove')" @click="removePattern(rule, pi)" />
                                </div>
                                <UButton variant="link" size="xs" icon="i-heroicons-plus" class="mt-1 -ms-2"
                                         @click="rule.patterns.push('')">{{ $t('settings.pii.addPattern') }}</UButton>
                            </div>
                        </div>
                    </div>
                </SettingsSection>

                <!-- Live test (the selected policy) -->
                <SettingsSection :title="$t('settings.pii.testTitle')" :description="$t('settings.pii.testDescPolicy', { policy: policyLabel(selected) })">
                    <UTextarea v-model="testInput" :rows="4" :placeholder="$t('settings.pii.testPlaceholder')" textarea-class="font-mono text-xs" />
                    <div class="mt-3">
                        <UButton size="sm" color="gray" icon="i-heroicons-play" :loading="testing" @click="runTest">
                            {{ $t('settings.pii.testButton') }}
                        </UButton>
                    </div>

                    <div v-if="testDone" class="mt-4">
                        <div v-if="testResult.blocked" class="rounded-lg border border-red-200 bg-red-50 dark:bg-red-950/40 dark:border-red-900/60 px-4 py-3 text-sm text-red-700 dark:text-red-300 flex items-center gap-2">
                            <Icon name="heroicons:no-symbol" class="w-4 h-4" />
                            {{ testResult.detector_unavailable ? $t('settings.pii.testDetectorUnavailable') : $t('settings.pii.testBlocked') }}
                        </div>
                        <template v-else>
                            <div class="text-xs font-medium text-ink-muted mb-1.5">{{ $t('settings.pii.testResult') }}</div>
                            <pre class="rounded-lg border border-line bg-surface-subtle px-4 py-3 font-mono text-xs text-ink-2 whitespace-pre-wrap break-words">{{ testResult.text }}</pre>
                        </template>
                        <p v-if="testResult.matches && testResult.matches.length" class="text-xs text-ink-muted mt-2">
                            {{ $t('settings.pii.testMatches', { rules: matchLabel }) }}
                        </p>
                        <p v-else-if="!testResult.blocked" class="text-xs text-green-700 dark:text-green-400 mt-2">{{ $t('settings.pii.testNoMatches') }}</p>
                    </div>
                </SettingsSection>
            </div>
        </template>
    </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import SettingsSection from '~/components/settings/SettingsSection.vue'
import SettingsRow from '~/components/settings/SettingsRow.vue'
import SettingsEmpty from '~/components/settings/SettingsEmpty.vue'
import SettingsLoading from '~/components/settings/SettingsLoading.vue'
import LLMProviderIcon from '~/components/LLMProviderIcon.vue'
import PiiActionHint from '~/components/settings/PiiActionHint.vue'

definePageMeta({ auth: true, permissions: ['manage_settings'] })

const { t } = useI18n()
const toast = useToast()
const { hasFeature } = useEnterprise()

const DEFAULT = 'default'
const EXEMPT = 'exempt'

const loading = ref(true)
const testing = ref(false)
const testDone = ref(false)
const testInput = ref('')
const testResult = ref<{ text: string; matches: any[]; blocked: boolean; detector_unavailable?: boolean }>({ text: '', matches: [], blocked: false })

type Kind = 'builtin' | 'custom'
interface RuleOptions { enabled?: boolean; action?: string; replacement?: string; min_score?: number }
interface Policy {
    name: string
    mode: string
    redact_context: boolean
    builtin_overrides: Record<string, RuleOptions>
    custom_overrides: Record<string, RuleOptions>
    detector_overrides: Record<string, RuleOptions>
    on_detector_error: string
}
interface CustomRule { id: string; name: string; patterns: string[]; replacement: string }

const emptyPolicy = (name: string): Policy => ({
    name, mode: 'replace', redact_context: false, builtin_overrides: {}, custom_overrides: {},
    detector_overrides: {}, on_detector_error: 'block',
})

const pii = reactive<{
    enabled: boolean
    custom_rules: CustomRule[]
    policies: Record<string, Policy>
    provider_policies: Record<string, string>
}>({
    enabled: false,
    custom_rules: [],
    policies: { [DEFAULT]: emptyPolicy('Default') },
    provider_policies: {},
})
const selected = ref<string>(DEFAULT)
const current = computed<Policy>(() => pii.policies[selected.value] || pii.policies[DEFAULT])
const providers = ref<Array<{ id: string; name: string; provider_type: string }>>([])
const providersLoaded = ref(false)
const builtinRules = ref<Array<{ id: string; name: string; replacement: string; pattern_count: number }>>([])
// The detector (Presidio) on this instance, if any: names, locations, organizations.
const detector = ref<{ available: boolean; name: string | null; entities: Array<{ id: string; name: string; replacement: string }> }>(
    { available: false, name: null, entities: [] }
)
const defaultMinScore = 0.7

const modeOptions = computed(() => [
    { value: 'replace', label: t('settings.pii.modeReplace'), description: t('settings.pii.modeReplaceDesc') },
    { value: 'block', label: t('settings.pii.modeBlock'), description: t('settings.pii.modeBlockDesc') },
])

const actionOptions = computed(() => [
    { label: t('settings.pii.actionReplace'), value: 'replace' },
    { label: t('settings.pii.actionPseudonymize'), value: 'pseudonymize' },
    { label: t('settings.pii.actionMask'), value: 'mask' },
    { label: t('settings.pii.actionBlock'), value: 'block' },
])

const detectorErrorOptions = computed(() => [
    { label: t('settings.pii.detectorErrorBlock'), value: 'block' },
    { label: t('settings.pii.detectorErrorRegexOnly'), value: 'regex_only' },
])

const matchLabel = computed(() =>
    (testResult.value.matches || []).map((m: any) => m.name + (m.count ? ` (${m.count})` : '')).join(', ')
)

// --- policies ---
const policyLabel = (id: string) =>
    id === DEFAULT ? t('settings.pii.defaultPolicy') : (pii.policies[id]?.name || id)

const policyOptions = computed(() => [
    ...Object.keys(pii.policies).map(id => ({ label: policyLabel(id), value: id })),
    { label: t('settings.pii.exemptOption'), value: EXEMPT },
])

const providerPolicy = (providerId: string) => {
    const p = pii.provider_policies[providerId]
    return p && (p === EXEMPT || pii.policies[p]) ? p : DEFAULT
}
const setProviderPolicy = (providerId: string, policyId: string) => {
    const next = { ...pii.provider_policies }
    if (!policyId || policyId === DEFAULT) delete next[providerId]
    else next[providerId] = policyId
    pii.provider_policies = next
}
const providerSummary = (providerId: string) => {
    const p = providerPolicy(providerId)
    return p === EXEMPT
        ? t('settings.pii.providerExempt')
        : t('settings.pii.providerUsesPolicy', { policy: policyLabel(p) })
}
const providerCount = (policyId: string) =>
    providers.value.filter(p => providerPolicy(p.id) === policyId).length

const addPolicy = () => {
    let n = Object.keys(pii.policies).length
    let id = `policy-${n}`
    while (pii.policies[id]) id = `policy-${++n}`
    // A new policy starts as a copy of Default.
    const base = JSON.parse(JSON.stringify(pii.policies[DEFAULT])) as Policy
    pii.policies = { ...pii.policies, [id]: { ...base, name: t('settings.pii.newPolicyName', { n }) } }
    selected.value = id
    save()
}
const removePolicy = () => {
    const id = selected.value
    if (id === DEFAULT) return
    if (!window.confirm(t('settings.pii.deletePolicyConfirm', { policy: policyLabel(id) }))) return
    const { [id]: _removed, ...rest } = pii.policies
    pii.policies = rest
    // Its providers go back to Default.
    pii.provider_policies = Object.fromEntries(
        Object.entries(pii.provider_policies).filter(([, p]) => p !== id)
    )
    selected.value = DEFAULT
    save()
}

// --- per-policy rule options (missing = rule default) ---
const overridesOf = (kind: Kind) =>
    kind === 'builtin' ? current.value.builtin_overrides : current.value.custom_overrides
const ruleEnabled = (kind: Kind, id: string) => overridesOf(kind)[id]?.enabled ?? true
const ruleAction = (kind: Kind, id: string) => overridesOf(kind)[id]?.action || current.value.mode
const ruleReplacement = (kind: Kind, id: string, fallback: string) => overridesOf(kind)[id]?.replacement || fallback
const setRuleOption = (kind: Kind, id: string, key: keyof RuleOptions, val: any) => {
    const map = overridesOf(kind)
    map[id] = { ...(map[id] || {}), [key]: val }
}

// --- detector entities (off unless a policy turns them on) ---
const detectorOption = (id: string): RuleOptions => current.value.detector_overrides[id] || {}
const detectorEnabled = (id: string) => detectorOption(id).enabled === true
const setDetectorOption = (id: string, key: keyof RuleOptions, val: any) => {
    current.value.detector_overrides[id] = { ...detectorOption(id), [key]: val }
}
const clampScore = (raw: any) => {
    const n = Number(raw)
    return Number.isFinite(n) ? Math.min(1, Math.max(0, Math.round(n * 100) / 100)) : defaultMinScore
}

// --- custom rules (shared patterns) ---
const addRule = () => {
    pii.custom_rules.push({
        id: `custom-${Date.now()}-${Math.floor(Math.random() * 1000)}`,
        name: '', patterns: [''], replacement: '[REDACTED]',
    })
}
const removeRule = (i: number) => {
    const [rule] = pii.custom_rules.splice(i, 1)
    if (rule) for (const p of Object.values(pii.policies)) delete p.custom_overrides[rule.id]
    save()
}
const removePattern = (rule: CustomRule, i: number) => {
    rule.patterns.splice(i, 1)
    if (rule.patterns.length === 0) rule.patterns.push('')
    save()
}

const buildConfig = () => {
    const rules = pii.custom_rules
        .map(r => ({ ...r, patterns: r.patterns.map(p => p.trim()).filter(Boolean) }))
        .filter(r => r.name.trim() || r.patterns.length)
    const ids = new Set(rules.map(r => r.id))
    const policies: Record<string, Policy> = {}
    for (const [id, p] of Object.entries(pii.policies)) {
        policies[id] = {
            ...p,
            name: id === DEFAULT ? 'Default' : (p.name || '').trim() || policyLabel(id),
            custom_overrides: Object.fromEntries(Object.entries(p.custom_overrides).filter(([rid]) => ids.has(rid))),
        }
    }
    const config: Record<string, any> = { enabled: pii.enabled, custom_rules: rules, policies }
    // Without the provider list the assignments are not resent, so a failed
    // load can never wipe them.
    if (providersLoaded.value) config.provider_policies = pii.provider_policies
    return config
}

const load = async () => {
    loading.value = true
    try {
        const [settingsRes, rulesRes, providersRes] = await Promise.all([
            useMyFetch('/api/organization/settings'),
            useMyFetch('/api/organization/pii/builtin-rules'),
            useMyFetch('/api/llm/providers'),
        ])
        const cfg = (settingsRes.data.value as any)?.config?.pii_protection
        if (cfg) {
            pii.enabled = !!cfg.enabled
            pii.custom_rules = (cfg.custom_rules || []).map((r: any) => ({
                id: r.id || `custom-${Math.random()}`,
                name: r.name || '',
                patterns: r.patterns && r.patterns.length ? [...r.patterns] : [''],
                replacement: r.replacement || '[REDACTED]',
            }))
            const policies: Record<string, Policy> = {}
            for (const [id, p] of Object.entries<any>(cfg.policies || {})) {
                policies[id] = {
                    name: p.name || id,
                    mode: p.mode || 'replace',
                    redact_context: !!p.redact_context,
                    builtin_overrides: p.builtin_overrides || {},
                    custom_overrides: p.custom_overrides || {},
                    detector_overrides: p.detector_overrides || {},
                    on_detector_error: p.on_detector_error || 'block',
                }
            }
            if (!policies[DEFAULT]) policies[DEFAULT] = emptyPolicy('Default')
            pii.policies = policies
            pii.provider_policies = { ...(cfg.provider_policies || {}) }
        }
        if (providersRes.status.value === 'success') {
            providers.value = ((providersRes.data.value as any[]) || [])
                .map(p => ({ id: String(p.id), name: p.name, provider_type: p.provider_type }))
            providersLoaded.value = true
        }
        builtinRules.value = (rulesRes.data.value as any)?.rules || []
        detector.value = (rulesRes.data.value as any)?.detector || { available: false, name: null, entities: [] }
    } finally {
        loading.value = false
    }
}

let saving = false
let saveQueued = false
const save = async () => {
    // A change made while a save is in flight is saved right after it, so a
    // quick second edit is never lost.
    if (saving) { saveQueued = true; return }
    saving = true
    saveQueued = false
    try {
        const res = await useMyFetch('/api/organization/settings', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config: { pii_protection: buildConfig() } }),
        })
        if (res.status.value !== 'success') {
            const detail = (res.error?.value as any)?.data?.detail || t('settings.pii.saveError')
            throw new Error(detail)
        }
        toast.add({ title: t('settings.pii.saved'), color: 'green', timeout: 2000 })
    } catch (e: any) {
        toast.add({ title: t('settings.pii.saveError'), description: e.message, color: 'red', timeout: 5000, icon: 'i-heroicons-exclamation-circle' })
    } finally {
        saving = false
        if (saveQueued) save()
    }
}

const runTest = async () => {
    testing.value = true
    testDone.value = false
    try {
        const res = await useMyFetch('/api/organization/pii/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: testInput.value, config: buildConfig(), policy_id: selected.value }),
        })
        if (res.status.value !== 'success') {
            const detail = (res.error?.value as any)?.data?.detail || t('settings.pii.testFailed')
            throw new Error(detail)
        }
        testResult.value = res.data.value as any
        testDone.value = true
    } catch (e: any) {
        toast.add({ title: t('settings.pii.testFailed'), description: e.message, color: 'red', timeout: 5000, icon: 'i-heroicons-exclamation-circle' })
    } finally {
        testing.value = false
    }
}

onMounted(load)
</script>
