import { reactive, ref, computed } from 'vue'

// PII protection settings, shared by the policies list (/settings/pii) and the
// policy editor (/settings/pii/<policy>): one copy of the config, one save queue.

export const PII_DEFAULT = 'default'
export const PII_EXEMPT = 'exempt'
// Agent assignment: "no agent policy" (only the provider's policy applies).
// The colon keeps both apart from policy ids (a policy may be named "none").
export const PII_NONE = ':none'
// Agent assignment: use the org's default agent policy (stored as no entry).
export const PII_INHERIT = ':inherit'
export const PII_DEFAULT_MIN_SCORE = 0.7

export type PiiRuleKind = 'builtin' | 'custom' | 'detector'
export interface PiiRuleOptions { enabled?: boolean; action?: string; replacement?: string; min_score?: number }
export interface PiiPolicy {
    name: string
    mode: string
    redact_context: boolean
    builtin_overrides: Record<string, PiiRuleOptions>
    custom_overrides: Record<string, PiiRuleOptions>
    detector_overrides: Record<string, PiiRuleOptions>
    on_detector_error: string
}
export interface PiiCustomRule { id: string; name: string; patterns: string[]; replacement: string }

const emptyPolicy = (name: string): PiiPolicy => ({
    name, mode: 'replace', redact_context: false, builtin_overrides: {}, custom_overrides: {},
    detector_overrides: {}, on_detector_error: 'block',
})

const pii = reactive<{
    enabled: boolean
    custom_rules: PiiCustomRule[]
    policies: Record<string, PiiPolicy>
    provider_policies: Record<string, string>
    agent_policies: Record<string, string>
    default_agent_policy: string
}>({
    enabled: false,
    custom_rules: [],
    policies: { [PII_DEFAULT]: emptyPolicy('Default') },
    provider_policies: {},
    agent_policies: {},
    default_agent_policy: PII_NONE,
})
const loading = ref(true)
const loaded = ref(false)
const providers = ref<Array<{ id: string; name: string; provider_type: string }>>([])
const providersLoaded = ref(false)
const agents = ref<Array<{ id: string; name: string; type: string }>>([])
const agentsLoaded = ref(false)
const builtinRules = ref<Array<{ id: string; name: string; replacement: string; pattern_count: number }>>([])
// The detector (Presidio) on this instance, if any: names, locations, organizations.
const detector = ref<{ available: boolean; name: string | null; entities: Array<{ id: string; name: string; replacement: string }> }>(
    { available: false, name: null, entities: [] }
)

let saving = false
let saveQueued = false

export function usePiiSettings() {
    const { t } = useI18n()
    const toast = useToast()

    const policyLabel = (id: string) =>
        id === PII_DEFAULT ? t('settings.pii.defaultPolicy') : (pii.policies[id]?.name || id)

    const providerPolicy = (providerId: string) => {
        const p = pii.provider_policies[providerId]
        return p && (p === PII_EXEMPT || pii.policies[p]) ? p : PII_DEFAULT
    }
    const setProviderPolicy = (providerId: string, policyId: string) => {
        const next = { ...pii.provider_policies }
        if (!policyId || policyId === PII_DEFAULT) delete next[providerId]
        else next[providerId] = policyId
        pii.provider_policies = next
    }
    const providersOf = (policyId: string) =>
        providers.value.filter(p => providerPolicy(p.id) === policyId)

    // --- per-agent policies (docs/design/pii-agent-policies.md) ---
    // The agent's own choice: a policy id, PII_NONE, or PII_INHERIT.
    const agentPolicy = (agentId: string) => {
        const p = pii.agent_policies[agentId]
        return p && (p === PII_NONE || pii.policies[p]) ? p : PII_INHERIT
    }
    const setAgentPolicy = (agentId: string, policyId: string) => {
        const next = { ...pii.agent_policies }
        if (!policyId || policyId === PII_INHERIT) delete next[agentId]
        else next[agentId] = policyId
        pii.agent_policies = next
    }
    // The policy the agent ends up with (PII_NONE when none).
    const effectiveAgentPolicy = (agentId: string) => {
        const own = agentPolicy(agentId)
        return own === PII_INHERIT ? pii.default_agent_policy : own
    }
    const agentsOf = (policyId: string) =>
        agents.value.filter(a => effectiveAgentPolicy(a.id) === policyId)

    // --- per-policy rule options (missing = rule default) ---
    const overridesOf = (policy: PiiPolicy, kind: PiiRuleKind) =>
        kind === 'builtin' ? policy.builtin_overrides
            : kind === 'custom' ? policy.custom_overrides : policy.detector_overrides
    const ruleOption = (policy: PiiPolicy, kind: PiiRuleKind, id: string): PiiRuleOptions =>
        overridesOf(policy, kind)[id] || {}
    // Pattern rules are on unless turned off; detector entities are off unless turned on.
    const ruleEnabled = (policy: PiiPolicy, kind: PiiRuleKind, id: string) =>
        kind === 'detector' ? ruleOption(policy, kind, id).enabled === true : ruleOption(policy, kind, id).enabled ?? true
    const ruleAction = (policy: PiiPolicy, kind: PiiRuleKind, id: string) =>
        ruleOption(policy, kind, id).action || policy.mode
    const setRuleOption = (policy: PiiPolicy, kind: PiiRuleKind, id: string, key: keyof PiiRuleOptions, val: any) => {
        const map = overridesOf(policy, kind)
        map[id] = { ...(map[id] || {}), [key]: val }
    }
    const enabledRuleCount = (policy: PiiPolicy) =>
        builtinRules.value.filter(r => ruleEnabled(policy, 'builtin', r.id)).length
        + pii.custom_rules.filter(r => ruleEnabled(policy, 'custom', r.id)).length
        + (detector.value.available ? detector.value.entities.filter(r => ruleEnabled(policy, 'detector', r.id)).length : 0)

    const newPolicyId = () => {
        let n = Object.keys(pii.policies).length
        let id = `policy-${n}`
        while (pii.policies[id]) id = `policy-${++n}`
        return id
    }
    const createPolicy = (name: string, copyFrom: string) => {
        const id = newPolicyId()
        const base = JSON.parse(JSON.stringify(pii.policies[copyFrom] || emptyPolicy(name))) as PiiPolicy
        pii.policies = { ...pii.policies, [id]: { ...base, name } }
        return id
    }
    const deletePolicy = (id: string) => {
        if (id === PII_DEFAULT) return
        const { [id]: _removed, ...rest } = pii.policies
        pii.policies = rest
        // Its providers go back to Default.
        pii.provider_policies = Object.fromEntries(
            Object.entries(pii.provider_policies).filter(([, p]) => p !== id)
        )
        // Its agents go back to the default agent policy.
        pii.agent_policies = Object.fromEntries(
            Object.entries(pii.agent_policies).filter(([, p]) => p !== id)
        )
        if (pii.default_agent_policy === id) pii.default_agent_policy = PII_NONE
    }
    const deleteCustomRule = (ruleId: string) => {
        pii.custom_rules = pii.custom_rules.filter(r => r.id !== ruleId)
        for (const p of Object.values(pii.policies)) delete p.custom_overrides[ruleId]
    }

    const buildConfig = () => {
        const rules = pii.custom_rules
            .map(r => ({ ...r, patterns: r.patterns.map(p => p.trim()).filter(Boolean) }))
            .filter(r => r.name.trim() || r.patterns.length)
        const ids = new Set(rules.map(r => r.id))
        const policies: Record<string, PiiPolicy> = {}
        for (const [id, p] of Object.entries(pii.policies)) {
            policies[id] = {
                ...p,
                name: id === PII_DEFAULT ? 'Default' : (p.name || '').trim() || policyLabel(id),
                custom_overrides: Object.fromEntries(Object.entries(p.custom_overrides).filter(([rid]) => ids.has(rid))),
            }
        }
        const config: Record<string, any> = { enabled: pii.enabled, custom_rules: rules, policies }
        // Without the provider list the assignments are not resent, so a failed
        // load can never wipe them.
        if (providersLoaded.value) config.provider_policies = pii.provider_policies
        // Same for agents.
        if (agentsLoaded.value) config.agent_policies = pii.agent_policies
        config.default_agent_policy = pii.default_agent_policy
        return config
    }

    const load = async () => {
        // A second page of the same settings shows what is already loaded while it refreshes.
        loading.value = !loaded.value
        try {
            const [settingsRes, rulesRes, providersRes, agentsRes] = await Promise.all([
                useMyFetch('/api/organization/settings'),
                useMyFetch('/api/organization/pii/builtin-rules'),
                useMyFetch('/api/llm/providers'),
                useMyFetch('/api/data_sources?show_all=true'),
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
                const policies: Record<string, PiiPolicy> = {}
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
                if (!policies[PII_DEFAULT]) policies[PII_DEFAULT] = emptyPolicy('Default')
                pii.policies = policies
                pii.provider_policies = { ...(cfg.provider_policies || {}) }
                pii.agent_policies = { ...(cfg.agent_policies || {}) }
                pii.default_agent_policy = cfg.default_agent_policy || PII_NONE
            }
            if (agentsRes.status.value === 'success') {
                const rows = (agentsRes.data.value as any)
                const list = Array.isArray(rows) ? rows : (rows?.items || rows?.data_sources || [])
                agents.value = list
                    .map((a: any) => ({ id: String(a.id), name: a.name, type: a.type }))
                    .sort((a: any, b: any) => (a.name || '').localeCompare(b.name || ''))
                agentsLoaded.value = true
            }
            if (providersRes.status.value === 'success') {
                providers.value = ((providersRes.data.value as any[]) || [])
                    .map(p => ({ id: String(p.id), name: p.name, provider_type: p.provider_type }))
                providersLoaded.value = true
            }
            builtinRules.value = (rulesRes.data.value as any)?.rules || []
            detector.value = (rulesRes.data.value as any)?.detector || { available: false, name: null, entities: [] }
            loaded.value = true
        } finally {
            loading.value = false
        }
    }

    const save = async (): Promise<boolean> => {
        // A change made while a save is in flight is saved right after it, so a
        // quick second edit is never lost.
        if (saving) { saveQueued = true; return true }
        saving = true
        saveQueued = false
        let ok = true
        try {
            const res = await useMyFetch('/api/organization/settings', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ config: { pii_protection: buildConfig() } }),
            })
            if (res.status.value !== 'success') {
                const detail = (res.error?.value as any)?.data?.detail || t('settings.pii.saveError')
                throw new Error(typeof detail === 'string' ? detail : t('settings.pii.saveError'))
            }
            toast.add({ title: t('settings.pii.saved'), color: 'green', timeout: 2000 })
        } catch (e: any) {
            ok = false
            toast.add({ title: t('settings.pii.saveError'), description: e.message, color: 'red', timeout: 5000, icon: 'i-heroicons-exclamation-circle' })
        } finally {
            saving = false
            if (saveQueued) save()
        }
        return ok
    }

    const preview = async (text: string, policyId: string) => {
        const res = await useMyFetch('/api/organization/pii/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text, config: buildConfig(), policy_id: policyId }),
        })
        if (res.status.value !== 'success') {
            const detail = (res.error?.value as any)?.data?.detail
            throw new Error(typeof detail === 'string' ? detail : t('settings.pii.testFailed'))
        }
        return res.data.value as { text: string; matches: any[]; blocked: boolean; detector_unavailable?: boolean }
    }

    const policyIds = computed(() => Object.keys(pii.policies))

    return {
        pii, loading, loaded, providers, agents, builtinRules, detector, policyIds,
        policyLabel, providerPolicy, setProviderPolicy, providersOf,
        agentPolicy, setAgentPolicy, effectiveAgentPolicy, agentsOf,
        ruleOption, ruleEnabled, ruleAction, setRuleOption, enabledRuleCount,
        createPolicy, deletePolicy, deleteCustomRule,
        load, save, preview,
    }
}
