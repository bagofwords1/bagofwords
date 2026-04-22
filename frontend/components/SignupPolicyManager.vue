<template>
    <div class="mt-4 max-w-2xl">
        <div>
            <h3 class="text-base font-medium text-gray-900">{{ $t('signupPolicy.title') }}</h3>
            <p class="text-sm text-gray-500 mt-1">
                {{ $t('signupPolicy.description') }}
            </p>

            <div
                v-if="globalUninvitedDisabled && form.enabled"
                class="mt-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800"
            >
                <b>{{ $t('signupPolicy.headsUpPrefix') }}</b> {{ $t('signupPolicy.headsUpMiddle') }}
                <code class="font-mono">allow_uninvited_signups</code> {{ $t('signupPolicy.headsUpFlagOff') }} <b>{{ $t('signupPolicy.headsUpOff') }}</b>{{ $t('signupPolicy.headsUpSuffix') }}
            </div>

            <!-- Enabled toggle -->
            <div class="mt-5 flex items-center justify-between">
                <div>
                    <div class="text-sm font-medium text-gray-900">{{ $t('signupPolicy.enable') }}</div>
                    <div class="text-xs text-gray-500">{{ $t('signupPolicy.enableHint') }}</div>
                </div>
                <UToggle v-model="form.enabled" />
            </div>

            <!-- Domains -->
            <div class="mt-5">
                <label class="block text-xs font-medium text-gray-600 mb-1.5">{{ $t('signupPolicy.allowedDomains') }}</label>
                <div class="flex items-center gap-2">
                    <input
                        v-model="domainInput"
                        type="text"
                        :placeholder="$t('signupPolicy.domainPlaceholder')"
                        class="flex-1 text-sm border border-gray-300 rounded-md px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                        @keydown.enter.prevent="addDomain"
                        @keydown.,.prevent="addDomain"
                    />
                    <UButton size="xs" variant="solid" color="blue" @click="addDomain">{{ $t('signupPolicy.add') }}</UButton>
                </div>
                <div v-if="form.allowed_domains.length" class="flex flex-wrap gap-2 mt-3">
                    <span
                        v-for="d in form.allowed_domains"
                        :key="d"
                        class="inline-flex items-center gap-1.5 text-xs bg-gray-100 text-gray-800 rounded-full px-2.5 py-1"
                    >
                        {{ d }}
                        <button
                            class="text-gray-500 hover:text-gray-700"
                            @click="removeDomain(d)"
                            :aria-label="$t('signupPolicy.removeDomainAria')"
                        >
                            <Icon name="heroicons:x-mark" class="h-3.5 w-3.5" />
                        </button>
                    </span>
                </div>
                <p class="mt-2 text-xs text-gray-500">
                    {{ $t('signupPolicy.domainsHint') }}
                </p>
            </div>

            <!-- Role -->
            <div class="mt-5">
                <label class="block text-xs font-medium text-gray-600 mb-1.5">{{ $t('signupPolicy.autoInviteRole') }}</label>
                <USelectMenu
                    v-if="roles.length"
                    v-model="form.auto_invite_role"
                    :options="roles.map((r) => r.name)"
                    size="sm"
                    class="w-60"
                />
                <input
                    v-else
                    v-model="form.auto_invite_role"
                    type="text"
                    class="w-60 text-sm border border-gray-300 rounded-md px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                />
                <p class="mt-2 text-xs text-gray-500">
                    {{ $t('signupPolicy.roleHint') }}
                </p>
            </div>

            <!-- Footer -->
            <div class="mt-6 flex items-center justify-between pt-4 border-t border-gray-100">
                <p class="text-xs text-gray-500">
                    {{ $t('signupPolicy.removeHint') }}
                </p>
                <UButton
                    color="blue"
                    size="sm"
                    :loading="saving"
                    :disabled="!isDirty"
                    @click="save"
                >
                    {{ $t('signupPolicy.save') }}
                </UButton>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { useAppSettings } from '~/composables/useAppSettings'
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
