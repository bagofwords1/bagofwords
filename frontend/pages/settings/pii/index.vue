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
                        <UToggle v-model="pii.enabled" data-testid="pii-enabled" @update:model-value="save" />
                    </SettingsRow>
                </SettingsSection>

                <!-- Policies: each opens its own page -->
                <SettingsSection :title="$t('settings.pii.policiesTitle')" :description="$t('settings.pii.policiesListDesc')">
                    <template #actions>
                        <UButton size="sm" color="gray" icon="i-heroicons-plus" :disabled="!pii.enabled"
                                 data-testid="pii-new-policy" @click="openNewPolicy">{{ $t('settings.pii.newPolicy') }}</UButton>
                    </template>

                    <div :class="['bow-table-card divide-y divide-line-subtle', { 'opacity-50 pointer-events-none': !pii.enabled }]"
                         data-testid="pii-policy-list">
                        <NuxtLink v-for="id in policyIds" :key="id" :to="`/settings/pii/${id}`"
                                  :data-testid="`pii-policy-${id}`"
                                  class="flex items-center justify-between gap-4 px-5 py-3 hover:bg-surface-subtle transition-colors">
                            <div class="min-w-0">
                                <div class="text-sm font-medium text-ink truncate">{{ policyLabel(id) }}</div>
                                <div class="text-xs text-ink-muted truncate">{{ usedBy(id) }}</div>
                            </div>
                            <div class="flex items-center gap-3 shrink-0 text-xs text-ink-muted">
                                <span class="nums">{{ $t('settings.pii.rulesOn', { n: enabledRuleCount(pii.policies[id]) }, enabledRuleCount(pii.policies[id])) }}</span>
                                <UIcon name="i-heroicons-chevron-right" class="w-4 h-4 rtl:rotate-180" />
                            </div>
                        </NuxtLink>
                    </div>
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
                                    <div class="text-sm font-medium text-ink truncate">{{ provider.name }}</div>
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

                <!-- Which policy each agent adds (strictest wins; see docs/design/pii-agent-policies.md) -->
                <SettingsSection :title="$t('settings.pii.agentsTitle')" :description="$t('settings.pii.agentsDesc')">
                    <div data-testid="pii-agent-policies" :class="['space-y-3', { 'opacity-50 pointer-events-none': !pii.enabled }]">
                        <div class="bow-table-card">
                            <div class="flex items-center justify-between gap-4 px-5 py-3">
                                <div class="min-w-0">
                                    <div class="text-sm font-medium text-ink">{{ $t('settings.pii.defaultAgentPolicy') }}</div>
                                    <div class="text-xs text-ink-muted">{{ $t('settings.pii.defaultAgentPolicyDesc') }}</div>
                                </div>
                                <USelect
                                    :model-value="pii.default_agent_policy"
                                    :options="defaultAgentOptions"
                                    :aria-label="$t('settings.pii.defaultAgentPolicy')"
                                    data-testid="pii-default-agent-policy"
                                    size="sm" class="w-80 max-w-[50%] shrink-0"
                                    @update:model-value="pii.default_agent_policy = $event; save()" />
                            </div>
                        </div>
                        <div v-if="agents.length === 0" class="rounded-lg border border-dashed border-line">
                            <SettingsEmpty icon="i-heroicons-circle-stack" :title="$t('settings.pii.noAgents')" compact />
                        </div>
                        <div v-else class="bow-table-card divide-y divide-line-subtle">
                            <div v-for="agent in agents" :key="agent.id" data-testid="pii-agent-row" :data-agent-id="agent.id"
                                 class="flex items-center justify-between gap-4 px-5 py-3">
                                <div class="flex items-center gap-3 min-w-0">
                                    <DataSourceIcon :type="agent.type" class="w-5 h-5 shrink-0" />
                                    <div class="text-sm font-medium text-ink truncate">{{ agent.name }}</div>
                                </div>
                                <USelect
                                    :model-value="agentPolicy(agent.id)"
                                    :options="agentOptions"
                                    :aria-label="$t('settings.pii.agentPolicyFor', { name: agent.name })"
                                    data-testid="pii-agent-policy"
                                    size="sm" class="w-80 max-w-[50%] shrink-0"
                                    @update:model-value="setAgentPolicy(agent.id, $event); save()" />
                            </div>
                        </div>
                    </div>
                </SettingsSection>

                <!-- Custom rules: defined once here, switched on per policy -->
                <SettingsSection :title="$t('settings.pii.customTitle')" :description="$t('settings.pii.customDescShared')">
                    <template #actions>
                        <UButton size="sm" color="gray" icon="i-heroicons-plus" :disabled="!pii.enabled"
                                 data-testid="pii-add-rule" @click="openRule(null)">{{ $t('settings.pii.addRule') }}</UButton>
                    </template>

                    <div :class="{ 'opacity-50 pointer-events-none': !pii.enabled }">
                        <div v-if="pii.custom_rules.length === 0" class="rounded-lg border border-dashed border-line">
                            <SettingsEmpty icon="i-heroicons-funnel" :title="$t('settings.pii.noCustomRules')" compact />
                        </div>
                        <div v-else class="bow-table-card divide-y divide-line-subtle" data-testid="pii-custom-rules">
                            <div v-for="rule in pii.custom_rules" :key="rule.id" data-testid="pii-custom-rule"
                                 class="flex items-center justify-between gap-4 px-5 py-3">
                                <div class="min-w-0">
                                    <div class="text-sm font-medium text-ink truncate">{{ rule.name || $t('settings.pii.ruleName') }}</div>
                                    <div class="text-xs text-ink-muted font-mono truncate" dir="ltr">{{ rule.patterns.filter(Boolean).join('  ·  ') }}</div>
                                </div>
                                <div class="flex items-center gap-1 shrink-0">
                                    <UButton color="gray" variant="ghost" size="sm" icon="i-heroicons-pencil-square"
                                             data-testid="pii-edit-rule" @click="openRule(rule)">{{ $t('common.edit') }}</UButton>
                                    <UButton color="red" variant="ghost" size="sm" icon="i-heroicons-trash"
                                             :aria-label="$t('settings.pii.removeRule')" data-testid="pii-delete-rule" @click="ruleToDelete = rule" />
                                </div>
                            </div>
                        </div>
                    </div>
                </SettingsSection>
            </div>
        </template>

        <!-- New policy -->
        <UModal v-model="showNewPolicy" :ui="{ width: 'sm:max-w-md' }">
            <div class="p-6" data-testid="pii-new-policy-modal">
                <h3 class="text-base font-semibold text-ink">{{ $t('settings.pii.newPolicy') }}</h3>
                <div class="mt-4 space-y-4">
                    <SettingsRow :label="$t('settings.pii.policyName')" stacked for-id="pii-new-policy-name">
                        <UInput id="pii-new-policy-name" v-model="newPolicyName" autofocus maxlength="60"
                                :placeholder="$t('settings.pii.policyNamePlaceholder')" @keydown.enter="confirmNewPolicy" />
                    </SettingsRow>
                    <SettingsRow :label="$t('settings.pii.copyFrom')" stacked for-id="pii-new-policy-copy">
                        <USelect id="pii-new-policy-copy" v-model="newPolicyCopy" :options="copyOptions" />
                    </SettingsRow>
                </div>
                <div class="mt-6 flex justify-end gap-2">
                    <UButton color="gray" variant="ghost" @click="showNewPolicy = false">{{ $t('common.cancel') }}</UButton>
                    <UButton color="blue" :disabled="!newPolicyName.trim()" data-testid="pii-create-policy" @click="confirmNewPolicy">{{ $t('common.create') }}</UButton>
                </div>
            </div>
        </UModal>

        <!-- Add / edit a custom rule -->
        <UModal v-model="showRule" :ui="{ width: 'sm:max-w-lg' }">
            <div class="p-6" data-testid="pii-rule-modal">
                <h3 class="text-base font-semibold text-ink">{{ editingRuleId ? $t('settings.pii.editRuleTitle') : $t('settings.pii.newRuleTitle') }}</h3>
                <div class="mt-4 space-y-4">
                    <SettingsRow :label="$t('settings.pii.ruleName')" stacked for-id="pii-rule-name">
                        <UInput id="pii-rule-name" v-model="draft.name" autofocus :placeholder="$t('settings.pii.ruleNamePlaceholder')" />
                    </SettingsRow>
                    <div>
                        <div class="text-sm font-medium text-ink">{{ $t('settings.pii.patterns') }}</div>
                        <p class="mt-0.5 text-xs text-ink-muted">{{ $t('settings.pii.patternsHelp') }}</p>
                        <div v-for="(_pat, pi) in draft.patterns" :key="pi" class="flex items-center gap-2 mt-1.5">
                            <UInput v-model="draft.patterns[pi]" size="sm" dir="ltr" data-testid="pii-rule-pattern"
                                    class="flex-1" input-class="font-mono text-xs" :placeholder="$t('settings.pii.patternPlaceholder')" />
                            <UButton color="gray" variant="ghost" size="xs" icon="i-heroicons-x-mark"
                                     :aria-label="$t('common.remove')" :disabled="draft.patterns.length === 1" @click="draft.patterns.splice(pi, 1)" />
                        </div>
                        <UButton variant="link" size="xs" icon="i-heroicons-plus" class="mt-1 -ms-2"
                                 @click="draft.patterns.push('')">{{ $t('settings.pii.addPattern') }}</UButton>
                    </div>
                    <SettingsRow :label="$t('settings.pii.replacement')" stacked for-id="pii-rule-replacement">
                        <UInput id="pii-rule-replacement" v-model="draft.replacement" dir="ltr" input-class="font-mono text-xs" placeholder="[REDACTED]" />
                    </SettingsRow>
                </div>
                <div class="mt-6 flex justify-end gap-2">
                    <UButton color="gray" variant="ghost" @click="showRule = false">{{ $t('common.cancel') }}</UButton>
                    <UButton color="blue" :disabled="!draftValid" :loading="savingRule" data-testid="pii-save-rule" @click="confirmRule">{{ $t('common.save') }}</UButton>
                </div>
            </div>
        </UModal>

        <!-- Delete a custom rule -->
        <UModal :model-value="!!ruleToDelete" :ui="{ width: 'sm:max-w-md' }" @update:model-value="v => { if (!v) ruleToDelete = null }">
            <div v-if="ruleToDelete" class="p-6">
                <h3 class="text-base font-semibold text-ink">{{ $t('settings.pii.deleteRuleTitle') }}</h3>
                <p class="mt-2 text-sm text-ink-muted leading-relaxed">{{ $t('settings.pii.deleteRuleBody', { name: ruleToDelete.name }) }}</p>
                <div class="mt-6 flex justify-end gap-2">
                    <UButton color="gray" variant="ghost" @click="ruleToDelete = null">{{ $t('common.cancel') }}</UButton>
                    <UButton color="red" data-testid="pii-confirm-delete-rule" @click="confirmDeleteRule">{{ $t('common.delete') }}</UButton>
                </div>
            </div>
        </UModal>
    </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import SettingsSection from '~/components/settings/SettingsSection.vue'
import SettingsRow from '~/components/settings/SettingsRow.vue'
import SettingsEmpty from '~/components/settings/SettingsEmpty.vue'
import SettingsLoading from '~/components/settings/SettingsLoading.vue'
import LLMProviderIcon from '~/components/LLMProviderIcon.vue'
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import { usePiiSettings, PII_DEFAULT, PII_EXEMPT, PII_NONE, PII_INHERIT, type PiiCustomRule } from '~/composables/usePiiSettings'

definePageMeta({ auth: true, permissions: ['manage_settings'] })

const { t } = useI18n()
const { hasFeature } = useEnterprise()
const {
    pii, loading, providers, agents, policyIds, policyLabel, providerPolicy, setProviderPolicy, providersOf,
    agentPolicy, setAgentPolicy, agentsOf,
    enabledRuleCount, createPolicy, deleteCustomRule, load, save,
} = usePiiSettings()

const policyOptions = computed(() => [
    ...policyIds.value.map(id => ({ label: policyLabel(id), value: id })),
    { label: t('settings.pii.exemptOption'), value: PII_EXEMPT },
])
// Agents can't be Exempt: an agent policy only adds redaction.
const defaultAgentOptions = computed(() => [
    { label: t('settings.pii.noAgentPolicy'), value: PII_NONE },
    ...policyIds.value.map(id => ({ label: policyLabel(id), value: id })),
])
const agentOptions = computed(() => [
    {
        label: t('settings.pii.inheritAgentPolicy', {
            policy: '\u2068' + (pii.default_agent_policy === PII_NONE ? t('settings.pii.noAgentPolicy') : policyLabel(pii.default_agent_policy)) + '\u2069',
        }),
        value: PII_INHERIT,
    },
    ...defaultAgentOptions.value,
])
const usedBy = (id: string) => {
    const names = providersOf(id).map(p => p.name)
    const agentCount = agentsOf(id).length
    const parts = []
    if (names.length) parts.push(t('settings.pii.usedBy', { providers: names.join(', ') }))
    if (agentCount) parts.push(t('settings.pii.usedByAgents', { n: agentCount }, agentCount))
    return parts.length ? parts.join(' · ') : t('settings.pii.usedByNone')
}

// --- new policy ---
const showNewPolicy = ref(false)
const newPolicyName = ref('')
const newPolicyCopy = ref(PII_DEFAULT)
const copyOptions = computed(() => policyIds.value.map(id => ({ label: policyLabel(id), value: id })))
const openNewPolicy = () => {
    newPolicyName.value = ''
    newPolicyCopy.value = PII_DEFAULT
    showNewPolicy.value = true
}
const confirmNewPolicy = async () => {
    const name = newPolicyName.value.trim()
    if (!name) return
    const id = createPolicy(name, newPolicyCopy.value)
    showNewPolicy.value = false
    await save()
    await navigateTo(`/settings/pii/${id}`)
}

// --- custom rules ---
const showRule = ref(false)
const savingRule = ref(false)
const editingRuleId = ref<string | null>(null)
const draft = reactive<{ name: string; patterns: string[]; replacement: string }>({ name: '', patterns: [''], replacement: '[REDACTED]' })
const draftValid = computed(() => !!draft.name.trim() && draft.patterns.some(p => p.trim()))
const ruleToDelete = ref<PiiCustomRule | null>(null)

const openRule = (rule: PiiCustomRule | null) => {
    editingRuleId.value = rule?.id || null
    draft.name = rule?.name || ''
    draft.patterns = rule ? [...rule.patterns] : ['']
    draft.replacement = rule?.replacement || '[REDACTED]'
    showRule.value = true
}
const confirmRule = async () => {
    if (!draftValid.value) return
    const before = JSON.parse(JSON.stringify(pii.custom_rules))
    const next = {
        name: draft.name.trim(),
        patterns: draft.patterns.map(p => p.trim()).filter(Boolean),
        replacement: draft.replacement.trim() || '[REDACTED]',
    }
    if (editingRuleId.value) {
        pii.custom_rules = pii.custom_rules.map(r => r.id === editingRuleId.value ? { ...r, ...next } : r)
    } else {
        pii.custom_rules = [...pii.custom_rules, { id: `custom-${Date.now()}-${Math.floor(Math.random() * 1000)}`, ...next }]
    }
    savingRule.value = true
    const ok = await save()
    savingRule.value = false
    // A pattern the server refuses (an invalid expression) keeps the modal open.
    if (ok) showRule.value = false
    else pii.custom_rules = before
}
const confirmDeleteRule = () => {
    if (!ruleToDelete.value) return
    deleteCustomRule(ruleToDelete.value.id)
    ruleToDelete.value = null
    save()
}

onMounted(load)
</script>
