<template>
    <div class="mt-6">
        <h2 class="text-lg font-medium text-gray-900">{{ $t('settings.general') }}
            <p class="text-sm text-gray-500 font-normal mb-8">
                {{ $t('settings.subtitle') }}
            </p>
        </h2>

        <div v-if="loading" class="py-4">
            <ULoader />
        </div>

        <UAlert v-if="error" class="mt-4" type="danger">
            {{ error }}
        </UAlert>

        <div v-if="!loading && !error" class="space-y-6">
            <!-- Organization Name -->
            <div class="md:w-2/3 space-y-2">
                <div class="text-sm font-medium text-gray-800">{{ $t('settings.organizationName') }}</div>
                <UInput v-model="form.organization_name" :maxlength="80" :placeholder="$t('settings.workspacePlaceholder')" />
            </div>
            <!-- Organization Icon -->
            <div class="md:w-2/3 space-y-2">
                <div class="text-sm font-medium text-gray-800">{{ $t('settings.organizationIcon') }}</div>
                <div class="flex items-center space-x-4">
                    <div class="w-20 h-14 rounded border bg-white overflow-hidden flex items-center justify-center">
                        <img v-if="form.icon_url" :src="form.icon_url" class="max-w-full max-h-full object-contain" />
                        <Icon v-else name="heroicons:building-office" class="w-6 h-6 text-gray-400" />
                    </div>
                    <div class="space-x-2">
                        <UButton size="sm" variant="outline" color="blue" @click="selectIcon">{{ form.icon_url ? $t('settings.changeIcon') : $t('settings.uploadIconButton') }}</UButton>
                        <UButton v-if="form.icon_url" size="sm" color="red" variant="soft" @click="queueRemoveIcon">{{ $t('common.remove') }}</UButton>
                        <input ref="fileInput" type="file" accept="image/*" class="hidden" @change="onIconSelected" />
                    </div>
                </div>
                <div class="text-xs text-gray-500">{{ $t('settings.iconConstraints') }}</div>
            </div>



            <!-- AI Analyst Name -->
            <div class="md:w-2/3 space-y-2">
                <div class="text-sm font-medium text-gray-800">{{ $t('settings.aiAnalystName') }}</div>
                <UInput v-model="form.ai_analyst_name" :maxlength="50" placeholder="AI Analyst" />
            </div>

            <!-- Credit toggle -->
            <div class="md:w-2/3 flex items-center justify-between">
                <div class="text-sm text-gray-800">{{ $t('settings.showCredit') }}</div>
                <UToggle v-model="form.bow_credit" />
            </div>

            <!-- Organization language -->
            <div class="md:w-2/3 space-y-2">
                <div class="text-sm font-medium text-gray-800">{{ $t('settings.language.label') }}</div>
                <USelect
                    v-model="form.locale"
                    :options="localeOptions"
                    option-attribute="label"
                    value-attribute="value"
                />
                <div class="text-xs text-gray-500">{{ $t('settings.language.description') }}</div>
            </div>

            <div class="md:w-2/3 pt-2">
                <UButton color="blue" @click="saveAll" :loading="saving">{{ $t('common.saveChanges') }}</UButton>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useToast } from '#imports'

const { t } = useI18n()

interface GeneralConfig {
    ai_analyst_name: string
    bow_credit: boolean
    icon_url?: string | null
    icon_key?: string | null
}

interface SettingsResponse {
    config?: { general?: GeneralConfig }
}

interface LocaleResponse {
    org_locale: string | null
    default_locale: string
    enabled_locales: string[]
    effective_locale: string
}

// Language labels rendered in their own language so a user can find
// their locale even while the UI is still in another language.
const LOCALE_NATIVE_LABELS: Record<string, string> = {
    en: 'English',
    es: 'Español',
    he: 'עברית',
}

definePageMeta({ auth: true, permissions: ['manage_settings'], layout: 'settings' })

const loading = ref(true)
const error = ref('')
const general = ref<GeneralConfig>({ ai_analyst_name: 'AI Analyst', bow_credit: true })
const form = ref<{ organization_name?: string; locale: string } & GeneralConfig>({
    ai_analyst_name: 'AI Analyst',
    bow_credit: true,
    locale: '',
})
// Empty string represents "no org override" (system default). Tracking the
// initial value lets saveAll skip the PUT when the user hasn't touched it.
const initialLocale = ref<string>('')
const enabledLocales = ref<string[]>([])
const systemDefaultLocale = ref<string>('en')
const pendingIconFile = ref<File | null>(null)
const removeIcon = ref(false)
const saving = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const toast = useToast()

const localeOptions = computed(() => {
    const defaultLabel = LOCALE_NATIVE_LABELS[systemDefaultLocale.value] || systemDefaultLocale.value
    const opts = [
        { label: t('settings.language.systemDefault', { locale: defaultLabel }), value: '' },
    ]
    for (const code of enabledLocales.value) {
        opts.push({ label: LOCALE_NATIVE_LABELS[code] || code, value: code })
    }
    return opts
})

const fetchSettings = async () => {
    loading.value = true
    error.value = ''
    try {
        const [settingsResp, localeResp] = await Promise.all([
            useMyFetch('/api/organization/settings'),
            useMyFetch('/api/organization/locale'),
        ])
        if (settingsResp.status.value !== 'success') throw new Error(settingsResp.error?.value?.data?.message || t('settings.failedToFetch'))
        const cfg = (settingsResp.data.value as SettingsResponse)?.config
        general.value = cfg?.general || { ai_analyst_name: 'AI Analyst', bow_credit: true }

        const loc = localeResp.data.value as LocaleResponse | null
        const orgLocale = loc?.org_locale ?? ''
        enabledLocales.value = loc?.enabled_locales ?? ['en']
        systemDefaultLocale.value = loc?.default_locale ?? 'en'
        initialLocale.value = orgLocale

        // Fetch current organization name from session if available
        const { organization } = useOrganization()
        form.value = { organization_name: organization.value?.name, locale: orgLocale, ...general.value }
    } catch (e: any) {
        error.value = e.message || t('settings.failedToLoad')
        toast.add({ title: t('common.error'), description: error.value, color: 'red' })
    } finally {
        loading.value = false
    }
}

const saveAll = async () => {
    saving.value = true
    try {
        // 1) If a new icon is selected or removal queued, handle icon first
        if (pendingIconFile.value) {
            const formData = new FormData()
            formData.append('icon', pendingIconFile.value)
            const upload = await useMyFetch('/api/organization/general/icon', { method: 'POST', body: formData })
            if (upload.status.value !== 'success') throw new Error(upload.error?.value?.data?.message || t('settings.uploadFailed'))
            const cfg = (upload.data.value as SettingsResponse)?.config
            form.value.icon_url = cfg?.general?.icon_url || form.value.icon_url
            form.value.icon_key = cfg?.general?.icon_key || form.value.icon_key
        } else if (removeIcon.value) {
            const remove = await useMyFetch('/api/organization/general/icon', { method: 'DELETE' })
            if (remove.status.value !== 'success') throw new Error(remove.error?.value?.data?.message || t('settings.removeFailed'))
            form.value.icon_url = null
            form.value.icon_key = null
        }

        // 2) Save organization name (if changed)
        if (form.value.organization_name) {
            await useMyFetch('/api/organization', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: form.value.organization_name }) })
        }

        // 3) Save textual and toggle settings
        const payload = { config: { general: { ai_analyst_name: form.value.ai_analyst_name, bow_credit: form.value.bow_credit, icon_key: form.value.icon_key, icon_url: form.value.icon_url } } }
        const response = await useMyFetch('/api/organization/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
        if (response.status.value !== 'success') throw new Error(response.error?.value?.data?.message || t('settings.failedToUpdate'))

        general.value = ((response.data.value as SettingsResponse)?.config?.general) || form.value

        // 4) Save org locale override (empty string clears it to system default).
        if (form.value.locale !== initialLocale.value) {
            const localeBody = JSON.stringify({ locale: form.value.locale || null })
            const localeResp = await useMyFetch('/api/organization/locale', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: localeBody })
            if (localeResp.status.value !== 'success') throw new Error(localeResp.error?.value?.data?.detail || t('settings.language.saveError'))
            const resolved = (localeResp.data?.value as any)?.effective_locale as string | undefined
            // Flip the admin's own view right away. Without this the reload below
            // would keep them on their prior locale (the plugin's hydration only
            // applies when bow.locale is unset, and pressing Save here is a
            // clear signal the admin wants to see the result).
            const setLocale = (useNuxtApp() as any).$setLocale as ((c: string) => void) | undefined
            if (resolved && typeof setLocale === 'function') setLocale(resolved)
            initialLocale.value = form.value.locale
        }

        toast.add({ title: t('settings.saved'), color: 'green' })
        // reload to reflect icon in default layout
        window.location.reload()
    } catch (e: any) {
        toast.add({ title: t('common.error'), description: e.message || t('settings.failedToSave'), color: 'red' })
    } finally {
        saving.value = false
        pendingIconFile.value = null
        removeIcon.value = false
    }
}

const selectIcon = () => fileInput.value?.click()

const onIconSelected = async (evt: Event) => {
    const input = evt.target as HTMLInputElement
    const file = input.files?.[0]
    if (!file) return
    if (file.size > 512 * 1024) {
        toast.add({ title: t('settings.iconTooLarge'), description: t('settings.iconMaxSize'), color: 'red' })
        return
    }
    pendingIconFile.value = file
    // show local preview immediately
    form.value.icon_url = URL.createObjectURL(file)
    removeIcon.value = false
    if (fileInput.value) fileInput.value.value = ''
}

const queueRemoveIcon = () => {
    form.value.icon_url = null
    form.value.icon_key = null
    pendingIconFile.value = null
    removeIcon.value = true
}

onMounted(fetchSettings)
</script>


