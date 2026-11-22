<template>
    <!-- A group's default and small default model, for an org LLM admin or a
         manager of the group (Enterprise llm_access_control). A manager's list
         holds only models the group can already use, so a default never widens
         what the group may use; the priority between groups stays on the LLM
         settings page, for admins. -->
    <div class="mb-4 rounded-lg border border-line p-3" data-testid="group-model-defaults">
        <div class="flex items-center gap-1.5 mb-1">
            <UIcon name="i-heroicons-cpu-chip" class="w-4 h-4 text-ink-muted" />
            <span class="text-sm font-medium text-ink">{{ $t('groupsManager.models.title') }}</span>
        </div>
        <p class="text-xs text-ink-muted mb-3">{{ $t('groupsManager.models.hint') }}</p>

        <div v-if="loading" class="text-sm text-ink-muted"><Spinner class="w-4 h-4 me-2 inline" />{{ $t('groupsManager.loading') }}</div>
        <div v-else class="grid grid-cols-1 gap-3">
            <label v-for="field in fields" :key="field.key" class="flex flex-col gap-1">
                <span class="text-xs font-medium text-ink-2">{{ field.label }}</span>
                <select
                    class="bow-native-control"
                    :value="row?.[field.key] || ''"
                    :disabled="saving"
                    :data-testid="field.testid"
                    @change="save(field.key, ($event.target as HTMLSelectElement).value || null)"
                >
                    <option value="">{{ $t('groupsManager.models.orgDefault') }}</option>
                    <option v-for="opt in optionsFor(field.key)" :key="opt.id" :value="opt.id">{{ opt.label }}</option>
                </select>
            </label>
        </div>
    </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import Spinner from './Spinner.vue'
import { useErrorMessage } from '~/composables/useErrorMessage'

const props = defineProps<{ groupId: string }>()
const emit = defineEmits(['updated'])

type Row = {
    group_id: string
    default_llm_model_id: string | null
    small_default_llm_model_id: string | null
    default_model: { id: string; name: string } | null
    small_default_model: { id: string; name: string } | null
}
type Choice = { id: string; name: string; provider_name?: string | null }
type FieldKey = 'default_llm_model_id' | 'small_default_llm_model_id'

const { t } = useI18n()
const toast = useToast()
const { getErrorMessage } = useErrorMessage()

const row = ref<Row | null>(null)
const choices = ref<Choice[]>([])
const loading = ref(false)
const saving = ref(false)

const fields = computed(() => [
    { key: 'default_llm_model_id' as FieldKey, label: t('groupsManager.models.default'), testid: 'group-default-model-select' },
    { key: 'small_default_llm_model_id' as FieldKey, label: t('groupsManager.models.small'), testid: 'group-small-model-select' },
])

const label = (c: Choice) => (c.provider_name ? `${c.name} · ${c.provider_name}` : c.name)

// The models offered, plus the current value when an admin chose one the
// group can't otherwise use (keeping it is allowed; picking it again after
// changing away is not).
function optionsFor(key: FieldKey) {
    const opts = choices.value.map(c => ({ id: c.id, label: label(c) }))
    const current = row.value?.[key]
    if (current && !opts.some(o => o.id === current)) {
        const named = key === 'default_llm_model_id' ? row.value?.default_model : row.value?.small_default_model
        opts.unshift({ id: current, label: t('groupsManager.models.setByAdmin', { model: named?.name || current }) })
    }
    return opts
}

async function load() {
    if (!props.groupId) return
    loading.value = true
    try {
        const [rows, opts] = await Promise.all([
            useMyFetch('/llm/group-defaults'),
            useMyFetch(`/llm/group-defaults/${props.groupId}/choices`),
        ])
        row.value = ((rows.data.value as Row[]) || []).find(r => r.group_id === props.groupId) || null
        choices.value = (opts.data.value as Choice[]) || []
    } finally {
        loading.value = false
    }
}

async function save(key: FieldKey, value: string | null) {
    saving.value = true
    try {
        const { data, error } = await useMyFetch(`/llm/group-defaults/${props.groupId}`, {
            method: 'PUT',
            body: { [key]: value },
        })
        if (error.value) {
            toast.add({ title: getErrorMessage(error.value, t('groupsManager.models.failed')), color: 'red' })
            await load() // show the server's real state
            return
        }
        row.value = data.value as Row
        toast.add({ title: t('groupsManager.models.saved'), color: 'green' })
        emit('updated')
    } finally {
        saving.value = false
    }
}

watch(() => props.groupId, load)
onMounted(load)
</script>
