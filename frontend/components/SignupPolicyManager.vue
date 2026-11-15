<template>
    <div>
        <!-- Page title + description come from the settings shell. -->
        <div
            v-if="globalUninvitedDisabled && form.enabled"
            class="mb-6 rounded-lg border border-amber-200 bg-amber-50/70 dark:border-amber-900/60 dark:bg-amber-950/40 px-4 py-3 text-sm text-amber-800 dark:text-amber-300"
        >
            <b>{{ $t('signupPolicy.headsUpPrefix') }}</b> {{ $t('signupPolicy.headsUpMiddle') }}
            <code class="font-mono text-xs">allow_uninvited_signups</code> {{ $t('signupPolicy.headsUpFlagOff') }} <b>{{ $t('signupPolicy.headsUpOff') }}</b>{{ $t('signupPolicy.headsUpSuffix') }}
        </div>

        <SettingsSection>
            <SettingsRow :label="$t('signupPolicy.enable')" :description="$t('signupPolicy.enableHint')">
                <UToggle v-model="form.enabled" />
            </SettingsRow>
        </SettingsSection>

        <SettingsSection :title="$t('signupPolicy.title')">
            <SettingsRow :label="$t('signupPolicy.allowedDomains')" :description="$t('signupPolicy.domainsHint')" stacked for-id="signup-domain">
                <div class="flex items-center gap-2">
                    <UInput
                        id="signup-domain"
                        v-model="domainInput"
                        class="flex-1"
                        :placeholder="$t('signupPolicy.domainPlaceholder')"
                        @keydown.enter.prevent="addDomain"
                        @keydown.,.prevent="addDomain"
                    />
                    <UButton color="gray" icon="i-heroicons-plus" @click="addDomain">{{ $t('signupPolicy.add') }}</UButton>
                </div>
                <div v-if="form.allowed_domains.length" class="flex flex-wrap gap-2 mt-3">
                    <span
                        v-for="d in form.allowed_domains"
                        :key="d"
                        class="inline-flex items-center gap-1 text-xs font-medium bg-surface-muted text-ink-2 rounded-md ps-2.5 pe-1 py-1"
                    >
                        {{ d }}
                        <button
                            type="button"
                            class="flex items-center justify-center w-4 h-4 rounded text-ink-muted hover:text-ink hover:bg-surface-subtle"
                            @click="removeDomain(d)"
                            :aria-label="$t('signupPolicy.removeDomainAria')"
                        >
                            <Icon name="heroicons:x-mark" class="h-3 w-3" />
                        </button>
                    </span>
                </div>
            </SettingsRow>

            <SettingsRow :label="$t('signupPolicy.autoInviteRole')" :description="$t('signupPolicy.roleHint')" stacked>
                <USelectMenu
                    v-if="roles.length"
                    v-model="form.auto_invite_role"
                    :options="roles.map((r) => r.name)"
                    class="w-64"
                />
                <UInput v-else v-model="form.auto_invite_role" class="w-64" />
            </SettingsRow>
        </SettingsSection>

        <SettingsFooter>
            <template #note>{{ $t('signupPolicy.removeHint') }}</template>
            <UButton color="blue" :loading="saving" :disabled="!isDirty" @click="save">
                {{ $t('signupPolicy.save') }}
            </UButton>
        </SettingsFooter>
    </div>
</template>

<script setup lang="ts">
import { useAppSettings } from '~/composables/useAppSettings'
import SettingsSection from '~/components/settings/SettingsSection.vue'
import SettingsRow from '~/components/settings/SettingsRow.vue'
import SettingsFooter from '~/components/settings/SettingsFooter.vue'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()

const props = defineProps<{
    organization: { id: string; name: string }
}>()

const toast = useToast()

type Policy = { enabled: boolean; allowed_domains: string[]; auto_invite_role: string }
const form = reactive<Policy>({ enabled: false, allowed_domains: [], auto_invite_role: 'member' })
const original = ref<Policy>({ enabled: false, allowed_domains: [], auto_invite_role: 'member' })

const domainInput = ref('')
const saving = ref(false)
const roles = ref<{ id: string; name: string }[]>([])

const { settings: appSettings, fetchSettings } = useAppSettings()
const globalUninvitedDisabled = computed(
    () => appSettings.value?.features?.allow_uninvited_signups === false,
)

const isDirty = computed(() => JSON.stringify(form) !== JSON.stringify(original.value))

async function loadPolicy() {
    try {
        const { data } = await useMyFetch('/organization/signup-policy')
        if (data.value) {
            const p = data.value as Policy
            Object.assign(form, p)
            original.value = JSON.parse(JSON.stringify(p))
        }
    } catch (e) {
        console.error('Failed to load signup policy', e)
    }
}

async function loadRoles() {
    try {
        const { data } = await useMyFetch(`/organizations/${props.organization.id}/roles`)
        if (data.value) roles.value = data.value as { id: string; name: string }[]
    } catch {
        roles.value = []
    }
}

function normalizeDomain(raw: string): string | null {
    const d = (raw || '').trim().toLowerCase()
    if (!d) return null
    if (d.includes('@') || d.includes('*') || /\s/.test(d)) return null
    if (!d.includes('.') || d.length > 253) return null
    return d
}

function addDomain() {
    const parts = domainInput.value.split(',')
    for (const p of parts) {
        const d = normalizeDomain(p)
        if (!d) continue
        if (!form.allowed_domains.includes(d)) form.allowed_domains.push(d)
    }
    domainInput.value = ''
}

function removeDomain(d: string) {
    form.allowed_domains = form.allowed_domains.filter((x) => x !== d)
}

async function save() {
    if (form.enabled && form.allowed_domains.length === 0) {
        toast.add({ title: t('signupPolicy.toastAddDomain'), color: 'amber' })
        return
    }
    saving.value = true
    try {
        const { data, error } = await useMyFetch('/organization/signup-policy', {
            method: 'PUT',
            body: { ...form },
        })
        if (error.value) throw error.value
        if (data.value) {
            const p = data.value as Policy
            Object.assign(form, p)
            original.value = JSON.parse(JSON.stringify(p))
            toast.add({ title: t('signupPolicy.toastSaved'), color: 'green' })
        }
    } catch (e: any) {
        const msg = e?.data?.detail || e?.message || t('signupPolicy.toastFailed')
        toast.add({ title: msg, color: 'red' })
    } finally {
        saving.value = false
    }
}

onMounted(() => {
    fetchSettings()
    loadPolicy()
    loadRoles()
})
</script>
