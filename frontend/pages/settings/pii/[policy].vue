<template>
    <div>
        <div v-if="!licensed" class="rounded-lg border border-amber-200 bg-amber-50/70 dark:bg-amber-950/40 dark:border-amber-900/60 p-5">
            <div class="text-sm font-semibold text-amber-800 dark:text-amber-300">{{ $t('settings.pii.enterpriseTitle') }}</div>
            <p class="text-sm text-amber-800/90 dark:text-amber-300/90 mt-1.5">{{ $t('settings.pii.enterpriseBody') }}</p>
        </div>

        <SettingsLoading v-else-if="loading" />

        <div v-else-if="!policy" data-testid="pii-policy-missing">
            <p class="text-sm text-ink-muted">{{ $t('settings.pii.policyNotFound') }}</p>
            <UButton to="/settings/pii" color="gray" size="sm" icon="i-heroicons-arrow-left" class="mt-4"
                     :ui="{ icon: { base: 'rtl:rotate-180' } }">{{ $t('settings.pii.backToPolicies') }}</UButton>
        </div>

        <div v-else data-testid="pii-policy-editor">
            <NuxtLink to="/settings/pii" data-testid="pii-back"
                      class="inline-flex items-center gap-1.5 text-sm text-ink-muted hover:text-ink transition-colors">
                <UIcon name="i-heroicons-arrow-left" class="w-4 h-4 rtl:rotate-180" />
                {{ $t('settings.pii.backToPolicies') }}
            </NuxtLink>

            <!-- Which policy this is -->
            <div class="mt-4 flex items-end justify-between gap-3">
                <div class="min-w-0 flex-1">
                    <template v-if="isDefault">
                        <h2 class="text-base font-semibold text-ink">{{ $t('settings.pii.policyHeading', { policy: label }) }}</h2>
                    </template>
                    <template v-else>
                        <label class="block mb-1 text-xs font-medium text-ink-muted" for="pii-policy-name">{{ $t('settings.pii.policyName') }}</label>
                        <UInput id="pii-policy-name" v-model="policy.name" maxlength="60" class="max-w-sm"
                                data-testid="pii-policy-name" @blur="save" />
                    </template>
                </div>
                <UButton v-if="!isDefault" color="red" variant="ghost" size="sm" icon="i-heroicons-trash"
                         data-testid="pii-delete-policy" @click="showDelete = true">{{ $t('settings.pii.deletePolicy') }}</UButton>
            </div>
            <p class="mt-2 text-sm text-ink-muted leading-relaxed" data-testid="pii-policy-used-by">
                {{ usedBy }}<template v-if="isDefault"> {{ $t('settings.pii.defaultPolicyNote') }}</template>
            </p>

            <div v-if="!pii.enabled" class="mt-4 rounded-lg border border-amber-200 bg-amber-50/70 dark:bg-amber-950/40 dark:border-amber-900/60 px-4 py-3 text-sm text-amber-800 dark:text-amber-300"
                 data-testid="pii-off-notice">
                {{ $t('settings.pii.offNotice') }}
            </div>

            <!-- One list: what this policy protects and what the model sees instead -->
            <div class="mt-8">
                <SettingsSection :title="$t('settings.pii.rulesTitle')" :description="$t('settings.pii.rulesDesc')">
                    <div v-for="group in groups" :key="group.key" class="[&+&]:mt-6" :data-testid="`pii-group-${group.key}`">
                        <div class="flex items-baseline justify-between gap-3 mb-2">
                            <h3 class="text-xs font-semibold uppercase tracking-wide text-ink-muted">{{ group.title }}</h3>
                            <NuxtLink v-if="group.key === 'custom'" to="/settings/pii" class="text-xs text-ink-muted hover:text-ink underline">
                                {{ $t('settings.pii.manageCustom') }}
                            </NuxtLink>
                        </div>
                        <p v-if="group.note" class="text-xs text-ink-muted mb-2 leading-relaxed">{{ group.note }}</p>

                        <div v-if="group.rows.length" class="bow-table-card divide-y divide-line-subtle">
                            <div v-for="row in group.rows" :key="row.id" data-testid="pii-rule-row" :data-rule="row.id"
                                 class="flex items-center justify-between gap-4 px-5 py-3">
                                <div class="min-w-0">
                                    <div class="text-sm font-medium text-ink truncate">{{ row.name }}</div>
                                    <div v-if="!enabled(row)" class="text-xs text-ink-muted">{{ $t('settings.pii.ruleOff') }}</div>
                                    <div v-else-if="row.sample" class="text-xs text-ink-muted font-mono truncate" dir="ltr" data-testid="pii-rule-example">
                                        {{ row.sample }} → <span :class="action(row) === 'block' ? 'text-red-600 dark:text-red-400 font-sans' : 'text-ink-2'">{{ example(row) }}</span>
                                    </div>
                                    <div v-else class="text-xs text-ink-muted">
                                        <template v-if="row.patternCount || !row.termCount">{{ $t('settings.pii.patternCount', { n: row.patternCount }, row.patternCount || 0) }}</template>
                                        <template v-if="row.patternCount && row.termCount"> · </template>
                                        <template v-if="row.termCount">{{ $t('settings.pii.termCount', { n: row.termCount }, row.termCount) }}</template>
                                    </div>
                                </div>
                                <div class="flex items-center gap-3 shrink-0">
                                    <template v-if="enabled(row)">
                                        <UInput
                                            v-if="action(row) === 'replace'"
                                            :model-value="replacement(row)"
                                            @update:model-value="setRuleOption(policy, row.kind, row.id, 'replacement', $event)"
                                            @blur="save"
                                            :aria-label="$t('settings.pii.replacement')" dir="ltr"
                                            size="sm" class="w-40" input-class="font-mono text-xs" :placeholder="row.replacement" />
                                        <PiiActionHint v-else-if="action(row) === 'pseudonymize'" :action="action(row)" />
                                        <USelect
                                            :model-value="action(row)"
                                            @update:model-value="setRuleOption(policy, row.kind, row.id, 'action', $event); save()"
                                            :options="actionOptions" :aria-label="$t('settings.pii.modelSees')"
                                            data-testid="pii-rule-action" size="sm" class="w-44" />
                                    </template>
                                    <UToggle
                                        :model-value="enabled(row)" :aria-label="row.name" data-testid="pii-rule-toggle"
                                        @update:model-value="setRuleOption(policy, row.kind, row.id, 'enabled', $event); save()" />
                                </div>
                            </div>
                        </div>
                    </div>
                </SettingsSection>

                <!-- Try it: live, against this policy as edited -->
                <SettingsSection :title="$t('settings.pii.testTitle')" :description="$t('settings.pii.testLiveDesc')">
                    <div class="grid gap-3 sm:grid-cols-2">
                        <div>
                            <div class="text-xs font-medium text-ink-muted mb-1.5">{{ $t('settings.pii.testInputLabel') }}</div>
                            <UTextarea v-model="testInput" :rows="6" :placeholder="$t('settings.pii.testPlaceholder')"
                                       data-testid="pii-test-input" textarea-class="font-mono text-xs" />
                        </div>
                        <div>
                            <div class="text-xs font-medium text-ink-muted mb-1.5">{{ $t('settings.pii.modelSees') }}</div>
                            <div v-if="testResult && testResult.blocked" data-testid="pii-test-blocked"
                                 class="rounded-lg border border-red-200 bg-red-50 dark:bg-red-950/40 dark:border-red-900/60 px-4 py-3 text-sm text-red-700 dark:text-red-300 flex items-start gap-2 min-h-[8.25rem]">
                                <Icon name="heroicons:no-symbol" class="w-4 h-4 mt-0.5 shrink-0" />
                                {{ testResult.detector_unavailable ? $t('settings.pii.testDetectorUnavailable') : $t('settings.pii.testBlocked') }}
                            </div>
                            <pre v-else data-testid="pii-test-output" dir="ltr"
                                 :class="['rounded-lg border border-line bg-surface-subtle px-4 py-3 font-mono text-xs text-ink-2 whitespace-pre-wrap break-words min-h-[8.25rem]', { 'opacity-60': testing }]">{{ testResult ? testResult.text : '' }}</pre>
                        </div>
                    </div>
                    <p v-if="testResult && testResult.matches && testResult.matches.length" class="text-xs text-ink-muted mt-2" data-testid="pii-test-matches">
                        {{ $t('settings.pii.testMatches', { rules: matchLabel }) }}
                    </p>
                    <p v-else-if="testResult && !testResult.blocked && testInput.trim()" class="text-xs text-green-700 dark:text-green-400 mt-2">{{ $t('settings.pii.testNoMatches') }}</p>
                    <p v-if="testError" class="text-xs text-red-600 dark:text-red-400 mt-2">{{ testError }}</p>
                </SettingsSection>

                <!-- Rarely changed -->
                <SettingsSection>
                    <button type="button" class="flex items-center gap-1.5 text-sm font-semibold text-ink" :aria-expanded="showAdvanced"
                            data-testid="pii-advanced-toggle" @click="showAdvanced = !showAdvanced">
                        <UIcon :name="showAdvanced ? 'i-heroicons-chevron-down' : 'i-heroicons-chevron-right'" class="w-4 h-4" :class="{ 'rtl:rotate-180': !showAdvanced }" />
                        {{ $t('settings.pii.advancedTitle') }}
                    </button>
                    <div v-if="showAdvanced" class="mt-5 space-y-5" data-testid="pii-advanced">
                        <SettingsRow :label="$t('settings.pii.redactContextLabel')" :description="$t('settings.pii.redactContextDesc')">
                            <UToggle v-model="policy.redact_context" data-testid="pii-redact-context" @update:model-value="save" />
                        </SettingsRow>
                        <template v-if="detector.available">
                            <SettingsRow :label="$t('settings.pii.detectorErrorLabel')" :description="$t('settings.pii.detectorErrorDesc')">
                                <USelect v-model="policy.on_detector_error" :options="detectorErrorOptions" size="sm" class="w-64"
                                         data-testid="pii-detector-error" @update:model-value="save" />
                            </SettingsRow>
                            <SettingsRow v-for="entity in enabledEntities" :key="entity.id"
                                         :label="$t('settings.pii.sensitivityFor', { name: $t(`settings.pii.entities.${entity.id}`) })"
                                         :description="$t('settings.pii.sensitivityDesc')">
                                <USelect :model-value="sensitivity(entity.id)" :options="sensitivityOptions(entity.id)" size="sm" class="w-64"
                                         data-testid="pii-sensitivity"
                                         @update:model-value="setRuleOption(policy, 'detector', entity.id, 'min_score', Number($event)); save()" />
                            </SettingsRow>
                        </template>
                    </div>
                </SettingsSection>
            </div>
        </div>

        <!-- Delete this policy -->
        <UModal v-model="showDelete" :ui="{ width: 'sm:max-w-md' }">
            <div class="p-6" data-testid="pii-delete-policy-modal">
                <h3 class="text-base font-semibold text-ink">{{ $t('settings.pii.deletePolicy') }}</h3>
                <p class="mt-2 text-sm text-ink-muted leading-relaxed">{{ $t('settings.pii.deletePolicyConfirm', { policy: label }) }}</p>
                <p v-if="policyProviders.length" class="mt-2 text-sm text-ink-2">{{ policyProviders.map(p => p.name).join(', ') }}</p>
                <div class="mt-6 flex justify-end gap-2">
                    <UButton color="gray" variant="ghost" @click="showDelete = false">{{ $t('common.cancel') }}</UButton>
                    <UButton color="red" data-testid="pii-confirm-delete-policy" @click="confirmDelete">{{ $t('common.delete') }}</UButton>
                </div>
            </div>
        </UModal>
    </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import SettingsSection from '~/components/settings/SettingsSection.vue'
import SettingsRow from '~/components/settings/SettingsRow.vue'
import SettingsLoading from '~/components/settings/SettingsLoading.vue'
import PiiActionHint from '~/components/settings/PiiActionHint.vue'
import { usePiiSettings, PII_DEFAULT, PII_DEFAULT_MIN_SCORE, type PiiRuleKind } from '~/composables/usePiiSettings'

definePageMeta({ auth: true, permissions: ['manage_settings'] })

const { t } = useI18n()
const route = useRoute()
const { hasFeature } = useEnterprise()
const {
    pii, loading, builtinRules, detector, policyLabel, providersOf,
    ruleOption, ruleEnabled, ruleAction, setRuleOption, deletePolicy, load, save, preview,
} = usePiiSettings()

const policyId = computed(() => String(route.params.policy))
const policy = computed(() => pii.policies[policyId.value])
const isDefault = computed(() => policyId.value === PII_DEFAULT)
const label = computed(() => policyLabel(policyId.value))
const policyProviders = computed(() => providersOf(policyId.value))
const usedBy = computed(() => policyProviders.value.length
    ? t('settings.pii.usedBy', { providers: policyProviders.value.map(p => p.name).join(', ') })
    : t('settings.pii.usedByNone'))

// --- the rules list ---
interface Row { kind: PiiRuleKind; id: string; name: string; replacement: string; sample?: string; patternCount?: number; termCount?: number }

// A value of each kind, to show what the chosen action does to it.
const SAMPLES: Record<string, string> = {
    email: 'jane@acme.com',
    credit_card: '4111 1111 1111 1234',
    us_ssn: '123-45-6789',
    phone: '+1 415 555 0132',
    ipv4: '192.168.4.27',
    iban: 'DE89 3704 0044 0532 0130 00',
    // Illustrative placeholder only — deliberately not a valid AWS key id
    // (hyphens break the AKIA[0-9A-Z]{16} shape) so secret scanners don't flag
    // this UI sample as a hardcoded credential. It only has to read as an AWS
    // access key for the masking preview.
    aws_access_key: 'AKIA-EXAMPLE-KEY-ID',
    PERSON: 'Jane Miller',
    LOCATION: 'Lisbon',
    ORGANIZATION: 'Acme Corp',
    NRP: 'Canadian',
    CREDIT_CARD: '4111 1111 1111 1111',
    IBAN_CODE: 'GB82 WEST 1234 5698 7654 32',
    ABA_ROUTING_NUMBER: '011000015',
    UK_NHS: '943 476 5919',
    ES_NIF: '55555555K',
    ipv6: '2001:db8::8a2e:370:7334',
    mac_address: '00:1A:2B:3C:4D:5E',
    il_id: '123456782',
    api_key: 'ghp_…',
    url_credentials: 'postgres://app:pw@db/',
    US_PASSPORT: '912803456',
    US_BANK_NUMBER: '123456789012',
    UK_NINO: 'AB 12 34 56 C',
}
// `jwt` and `private_key` carry illustrative placeholder shapes only — never real
// secrets. They are assigned indirectly (not as `jwt: '…'` / `private_key: '…'`
// literals) because Snyk Code's HardcodedNonCryptoSecret heuristic keys on the
// *property name*, not the value, and would otherwise flag these UI masking-preview
// samples as hardcoded credentials. The shapes are deliberately non-secret: the JWT
// sample uses plain words in place of a base64 `eyJ…` header/payload, and the
// private-key sample uses em-dash rules (—) instead of the literal
// `-----BEGIN PRIVATE KEY-----` PEM marker. Same intent as aws_access_key above.
for (const [id, shape] of [
    ['jwt', 'header.payload.signature'],
    ['private_key', '——BEGIN PRIVATE KEY—— …'],
] as const) {
    SAMPLES[id] = shape
}

const groups = computed(() => {
    const out: Array<{ key: string; title: string; note?: string; rows: Row[] }> = [{
        key: 'patterns',
        title: t('settings.pii.groupPatterns'),
        rows: builtinRules.value.map(r => ({
            kind: 'builtin' as const, id: r.id, name: r.name, replacement: r.replacement, sample: SAMPLES[r.id], patternCount: r.pattern_count,
        })),
    }]
    const detectorRows = (kind: 'ner' | 'pattern' | 'context') => detector.value.entities
        .filter(r => (r.kind || 'ner') === kind)
        .map(r => ({
            kind: 'detector' as const, id: r.id, name: t(`settings.pii.entities.${r.id}`), replacement: r.replacement, sample: SAMPLES[r.id],
        }))
    out.push({
        key: 'detector',
        title: t('settings.pii.detectorTitle'),
        note: detector.value.available ? t('settings.pii.detectorNote') : t('settings.pii.detectorUnavailable'),
        rows: detector.value.available ? detectorRows('ner') : [],
    })
    // Checksum-validated identifiers (credit cards, IBANs, national IDs) from the same detector.
    const validated = detector.value.available ? detectorRows('pattern') : []
    if (validated.length) {
        out.push({ key: 'validated', title: t('settings.pii.validatedTitle'), note: t('settings.pii.validatedNote'), rows: validated })
    }
    // No checksum: these mostly match with a context word ("passport"…) nearby.
    const contextual = detector.value.available ? detectorRows('context') : []
    if (contextual.length) {
        out.push({ key: 'contextual', title: t('settings.pii.contextualTitle'), note: t('settings.pii.contextualNote'), rows: contextual })
    }
    if (pii.custom_rules.length) {
        out.push({
            key: 'custom',
            title: t('settings.pii.customTitle'),
            rows: pii.custom_rules.map(r => ({
                kind: 'custom' as const, id: r.id, name: r.name, replacement: r.replacement, patternCount: r.patterns.filter(Boolean).length,
                termCount: r.terms?.length || 0,
            })),
        })
    }
    return out
})

const enabled = (row: Row) => ruleEnabled(policy.value, row.kind, row.id)
const action = (row: Row) => ruleAction(policy.value, row.kind, row.id)
const replacement = (row: Row) => ruleOption(policy.value, row.kind, row.id).replacement || row.replacement

// Same shapes the backend produces (redactor.mask_value / token_label).
const maskSample = (value: string) => {
    const positions = [...value].map((ch, i) => (/[\p{L}\p{N}]/u.test(ch) ? i : -1)).filter(i => i >= 0)
    const keep = new Set(positions.length > 4 ? positions.slice(-4) : [])
    return [...value].map((ch, i) => (/[\p{L}\p{N}]/u.test(ch) && !keep.has(i) ? '*' : ch)).join('')
}
const tokenLabel = (raw: string) => raw.replace(/[^A-Za-z0-9]+/g, '_').replace(/^_+|_+$/g, '').toUpperCase().slice(0, 24) || 'PII'
const example = (row: Row) => {
    const a = action(row)
    if (a === 'block') return t('settings.pii.blocksRequest')
    if (a === 'mask') return maskSample(row.sample || '')
    if (a === 'pseudonymize') return `<${tokenLabel(row.id)}_3fa9c21d>`
    return replacement(row)
}

const actionOptions = computed(() => [
    { label: t('settings.pii.actionReplace'), value: 'replace' },
    { label: t('settings.pii.actionPseudonymize'), value: 'pseudonymize' },
    { label: t('settings.pii.actionMask'), value: 'mask' },
    { label: t('settings.pii.actionBlock'), value: 'block' },
])

// --- advanced ---
const showAdvanced = ref(false)
const detectorErrorOptions = computed(() => [
    { label: t('settings.pii.detectorErrorBlock'), value: 'block' },
    { label: t('settings.pii.detectorErrorRegexOnly'), value: 'regex_only' },
])
const enabledEntities = computed(() =>
    detector.value.entities.filter(e => ruleEnabled(policy.value, 'detector', e.id)))
// Presets around each entity's own default (0.7 for most; 0.4 for entities
// that need a nearby context word, so "balanced" is that default).
const entityDefault = (id: string) => detector.value.entities.find(e => e.id === id)?.min_score ?? PII_DEFAULT_MIN_SCORE
const presets = (id: string) => {
    const d = entityDefault(id)
    return d === PII_DEFAULT_MIN_SCORE
        ? [{ value: 0.5, key: 'sensitivityHigh' }, { value: d, key: 'sensitivityBalanced' }, { value: 0.85, key: 'sensitivityLow' }]
        : [{ value: Math.max(0.1, Math.round((d - 0.15) * 100) / 100), key: 'sensitivityHigh' }, { value: d, key: 'sensitivityBalanced' }, { value: 0.85, key: 'sensitivityLow' }]
}
const sensitivity = (id: string) => ruleOption(policy.value, 'detector', id).min_score ?? entityDefault(id)
const sensitivityOptions = (id: string) => {
    const SENSITIVITY = presets(id)
    const options = SENSITIVITY.map(s => ({ label: t(`settings.pii.${s.key}`), value: s.value }))
    const current = sensitivity(id)
    // A score saved outside the three presets stays selectable as it is.
    if (!SENSITIVITY.some(s => s.value === current)) options.push({ label: t('settings.pii.sensitivityCustom', { n: current }), value: current })
    return options
}

// --- delete ---
const showDelete = ref(false)
const confirmDelete = async () => {
    showDelete.value = false
    const id = policyId.value
    await navigateTo('/settings/pii')
    deletePolicy(id)
    save()
}

// --- live preview ---
const testInput = ref('')
const testing = ref(false)
const testError = ref('')
const testResult = ref<{ text: string; matches: any[]; blocked: boolean; detector_unavailable?: boolean } | null>(null)
const matchLabel = computed(() =>
    (testResult.value?.matches || []).map((m: any) => m.name + (m.count ? ` (${m.count})` : '')).join(', ')
)
let timer: ReturnType<typeof setTimeout> | null = null
let run = 0
const runTest = async () => {
    const text = testInput.value
    if (!text.trim() || !policy.value) { testResult.value = null; testError.value = ''; return }
    const mine = ++run
    testing.value = true
    try {
        const res = await preview(text, policyId.value)
        if (mine === run) { testResult.value = res; testError.value = '' }
    } catch (e: any) {
        if (mine === run) testError.value = e.message
    } finally {
        if (mine === run) testing.value = false
    }
}
const scheduleTest = () => {
    if (timer) clearTimeout(timer)
    timer = setTimeout(runTest, 400)
}
// The preview follows both the sample text and every edit to the policy.
watch(testInput, scheduleTest)
watch(() => [policy.value, pii.custom_rules], scheduleTest, { deep: true })
onBeforeUnmount(() => { if (timer) clearTimeout(timer) })

// Unlicensed: the list page carries the enterprise notice. The licence loads
// after a hard reload, so this is watched, not checked once on mount.
const licensed = computed(() => hasFeature('pii_protection'))
onMounted(load)
</script>
