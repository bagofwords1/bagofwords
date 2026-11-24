<template>
    <!-- The agent's PII policy (docs/design/pii-agent-policies.md). Agent
         managers pick one of the org's policies or inherit the org default;
         turning it off is for org admins (the server enforces it too). -->
    <div v-if="state && state.licensed" class="border-b border-gray-100 dark:border-gray-800 pb-5 mb-5" data-testid="agent-pii-policy">
        <label for="agent-pii-policy" class="block text-2xs font-semibold uppercase tracking-wider text-ink-muted mb-2">{{ t('agentAccess.pii.title') }}</label>
        <USelect
            id="agent-pii-policy"
            :model-value="state.policy"
            :options="options"
            :disabled="disabled || saving || !state.enabled"
            size="sm"
            class="max-w-[20rem]"
            data-testid="agent-pii-policy-select"
            @update:model-value="onChange"
        />
        <p class="text-2xs text-ink-muted mt-2" data-testid="agent-pii-policy-hint">
            <template v-if="!state.enabled">{{ t('agentAccess.pii.disabled') }}</template>
            <template v-else>{{ t('agentAccess.pii.hint') }}</template>
        </p>
    </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

const props = defineProps<{ agentId: string; disabled?: boolean }>()
const { t } = useI18n()
const toast = useToast()

interface PiiState {
    licensed: boolean
    enabled: boolean
    policy: string
    default_agent_policy: string
    default_agent_policy_name: string | null
    effective_policy: string
    policies: Array<{ id: string; name: string }>
    can_turn_off: boolean
}
const state = ref<PiiState | null>(null)
const saving = ref(false)

const policyName = (p: { id: string; name: string }) => (p.id === 'default' ? t('settings.pii.defaultPolicy') : p.name)

const options = computed(() => {
    const s = state.value
    if (!s) return []
    const inheritName = s.default_agent_policy === 'none' || !s.default_agent_policy_name
        ? t('settings.pii.noAgentPolicy')
        : (s.default_agent_policy === 'default' ? t('settings.pii.defaultPolicy') : s.default_agent_policy_name)
    // FSI … PDI: a Latin policy name keeps its own order inside an RTL label.
    const out = [{ label: t('settings.pii.inheritAgentPolicy', { policy: `\u2068${inheritName}\u2069` }), value: 'inherit' }]
    // "No agent policy" can only loosen the org default: org admins only.
    if (s.can_turn_off || s.policy === 'none') {
        out.push({ label: t('settings.pii.noAgentPolicy'), value: 'none', disabled: !s.can_turn_off } as any)
    }
    return [...out, ...s.policies.map(p => ({ label: policyName(p), value: p.id }))]
})

async function load() {
    const id = props.agentId
    if (!id) return
    const { data, error } = await useMyFetch(`/data_sources/${id}/pii_policy`, { method: 'GET' })
    if (id !== props.agentId) return
    state.value = error?.value ? null : (data.value as PiiState)
}

async function onChange(policy: string) {
    if (!state.value || policy === state.value.policy) return
    saving.value = true
    try {
        const { data, error } = await useMyFetch(`/data_sources/${props.agentId}/pii_policy`, {
            method: 'PUT',
            body: { policy },
        })
        if (error?.value) {
            const detail = (error.value as any)?.data?.detail
            toast.add({ title: t('agentAccess.pii.saveError'), description: typeof detail === 'string' ? detail : undefined, color: 'red' })
            return
        }
        state.value = data.value as PiiState
        toast.add({ title: t('agentAccess.pii.saved'), color: 'green', timeout: 2000 })
    } finally {
        saving.value = false
    }
}

watch(() => props.agentId, load, { immediate: true })
</script>
