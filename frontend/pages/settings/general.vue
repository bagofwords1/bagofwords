<template>
    <div>
        <SettingsLoading v-if="loading" />

        <UAlert v-if="error" color="red" variant="soft" icon="i-heroicons-exclamation-triangle" :title="error" />

        <div v-if="!loading && !error">
            <SettingsSection :title="$t('settings.generalSections.workspace')" :description="$t('settings.generalSections.workspaceHint')">
                <SettingsRow :label="$t('settings.organizationName')" stacked for-id="org-name">
                    <UInput id="org-name" v-model="form.organization_name" :maxlength="80" :placeholder="$t('settings.workspacePlaceholder')" />
                </SettingsRow>
                <SettingsRow :label="$t('settings.organizationIcon')" :description="$t('settings.iconConstraints')" stacked>
                    <div class="flex items-center gap-4">
                        <div class="w-16 h-16 rounded-lg border border-line bg-surface-subtle overflow-hidden flex items-center justify-center shrink-0">
                            <img v-if="form.icon_url" :src="form.icon_url" alt="" class="max-w-full max-h-full object-contain" />
                            <UIcon v-else name="i-heroicons-building-office" class="w-6 h-6 text-ink-subtle" />
                        </div>
                        <div class="flex items-center gap-2">
                            <UButton size="sm" color="gray" variant="solid" icon="i-heroicons-arrow-up-tray" @click="selectIcon">{{ form.icon_url ? $t('settings.changeIcon') : $t('settings.uploadIconButton') }}</UButton>
                            <UButton v-if="form.icon_url" size="sm" color="red" variant="ghost" @click="queueRemoveIcon">{{ $t('common.remove') }}</UButton>
                            <input ref="fileInput" type="file" accept="image/*" class="hidden" @change="onIconSelected" />
                        </div>
                    </div>
                </SettingsRow>
            </SettingsSection>

            <SettingsSection :title="$t('settings.generalSections.assistant')" :description="$t('settings.generalSections.assistantHint')">
                <SettingsRow :label="$t('settings.aiAnalystName')" stacked for-id="ai-name">
                    <UInput id="ai-name" v-text-dir v-model="form.ai_analyst_name" :maxlength="50" placeholder="AI Analyst" />
                </SettingsRow>
                <SettingsRow :label="$t('settings.showCredit')">
                    <UToggle v-model="form.bow_credit" />
                </SettingsRow>
            </SettingsSection>

            <SettingsSection :title="$t('settings.generalSections.region')" :description="$t('settings.generalSections.regionHint')">
                <SettingsRow :label="$t('settings.language.label')" :description="$t('settings.language.description')" stacked>
                    <USelect v-model="form.locale" :options="localeOptions" option-attribute="label" value-attribute="value" />
                </SettingsRow>
                <SettingsRow :label="$t('settings.timezone.label')" :description="$t('settings.timezone.description')" stacked>
                    <USelect v-model="form.timezone" :options="timezoneOptions" option-attribute="label" value-attribute="value" searchable />
                </SettingsRow>
                <SettingsRow :label="$t('settings.weekStart.label')" :description="$t('settings.weekStart.description')" stacked>
                    <USelect v-model="form.week_start" :options="weekStartOptions" option-attribute="label" value-attribute="value" />
                </SettingsRow>
            </SettingsSection>

            <SettingsSection v-if="productUpdates.available" :title="$t('productUpdates.settingsTitle')">
                <SettingsRow
                    :label="$t('productUpdates.toggleLabel')"
                    :description="form.product_updates
                        ? $t('productUpdates.sentTo', { email: productUpdates.email || productUpdates.current_user_email })
                        : undefined"
                >
                    <UToggle v-model="form.product_updates" data-testid="product-updates-toggle" />
                </SettingsRow>
            </SettingsSection>

            <SettingsFooter>
                <UButton color="blue" :loading="saving" @click="saveAll">{{ $t('common.saveChanges') }}</UButton>
            </SettingsFooter>
        </div>
    </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useToast } from '#imports'
import SettingsSection from '~/components/settings/SettingsSection.vue'
import SettingsRow from '~/components/settings/SettingsRow.vue'
import SettingsFooter from '~/components/settings/SettingsFooter.vue'
import SettingsLoading from '~/components/settings/SettingsLoading.vue'

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
    fr: 'Français',
    sv: 'Svenska',
    ar: 'العربية',
    ru: 'Русский',
    de: 'Deutsch',
    pt: 'Português (Brasil)',
    it: 'Italiano',
}

definePageMeta({ auth: true, permissions: ['manage_settings'] })

const loading = ref(true)
const error = ref('')
const general = ref<GeneralConfig>({ ai_analyst_name: 'AI Analyst', bow_credit: true })
const form = ref<{ organization_name?: string; locale: string; timezone: string; week_start: string; product_updates: boolean } & GeneralConfig>({
    ai_analyst_name: 'AI Analyst',
    bow_credit: true,
    locale: '',
    timezone: '',
    week_start: '',
    product_updates: false,
})
// Instance-wide opt-in to release notes and product updates. Hidden when the
// instance can't send it (telemetry off, cloud) or the caller can't change it.
interface ProductUpdatesStatus {
    available: boolean
    decided: boolean
    opted_in: boolean
    email: string | null
    current_user_email: string | null
}
const productUpdates = ref<ProductUpdatesStatus>({ available: false, decided: false, opted_in: false, email: null, current_user_email: null })
// Empty string represents "no org override" (system default). Tracking the
// initial value lets saveAll skip the PUT when the user hasn't touched it.
const initialLocale = ref<string>('')
const enabledLocales = ref<string[]>([])
const systemDefaultLocale = ref<string>('en')
// Timezone: '' === no override (UTC). Track initial to skip an untouched PUT.
const initialTimezone = ref<string>('')
const supportedTimezones = ref<string[]>([])
// Work week start: '' === auto (derive from locale). Track initial to skip an
// untouched PUT. ``effectiveWeekStart`` is what the AI actually uses.
const initialWeekStart = ref<string>('')
const effectiveWeekStart = ref<string>('monday')
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

const timezoneOptions = computed(() => {
    const opts = [{ label: t('settings.timezone.systemDefault'), value: '' }]
    for (const tz of supportedTimezones.value) {
        opts.push({ label: tz, value: tz })
    }
    return opts
})

const weekStartOptions = computed(() => {
    // Auto shows the locale-derived value so admins know what the AI will use.
    const autoLabel = t('settings.weekStart.auto', {
        day: t(`settings.weekStart.days.${effectiveWeekStart.value}`),
    })
    return [
        { label: autoLabel, value: '' },
        { label: t('settings.weekStart.days.sunday'), value: 'sunday' },
        { label: t('settings.weekStart.days.monday'), value: 'monday' },
        { label: t('settings.weekStart.days.saturday'), value: 'saturday' },
    ]
})

const fetchSettings = async () => {
    loading.value = true
    error.value = ''
    try {
        const [settingsResp, localeResp, tzResp, tzListResp, weekResp, updatesResp] = await Promise.all([
            useMyFetch('/api/organization/settings'),
            useMyFetch('/api/organization/locale'),
            useMyFetch('/api/organization/timezone'),
            useMyFetch('/api/organization/timezones'),
            useMyFetch('/api/organization/week_start'),
            useMyFetch('/api/product-updates'),
        ])
        if (updatesResp.status.value === 'success' && updatesResp.data.value) {
            productUpdates.value = updatesResp.data.value as ProductUpdatesStatus
        }
        if (settingsResp.status.value !== 'success') throw new Error(settingsResp.error?.value?.data?.message || t('settings.failedToFetch'))
        const cfg = (settingsResp.data.value as SettingsResponse)?.config
        general.value = cfg?.general || { ai_analyst_name: 'AI Analyst', bow_credit: true }

        const loc = localeResp.data.value as LocaleResponse | null
        const orgLocale = loc?.org_locale ?? ''
        enabledLocales.value = loc?.enabled_locales ?? ['en']
        systemDefaultLocale.value = loc?.default_locale ?? 'en'
        initialLocale.value = orgLocale

        const tz = tzResp.data.value as { org_timezone?: string | null } | null
        const orgTimezone = tz?.org_timezone ?? ''
        initialTimezone.value = orgTimezone
        supportedTimezones.value = (tzListResp.data.value as { timezones?: string[] } | null)?.timezones ?? []

        const week = weekResp.data.value as { org_week_start?: string | null; effective_week_start?: string } | null
        const orgWeekStart = week?.org_week_start ?? ''
        initialWeekStart.value = orgWeekStart
        effectiveWeekStart.value = week?.effective_week_start ?? 'monday'

        // Fetch current organization name from session if available
        const { organization } = useOrganization()
        form.value = { organization_name: organization.value?.name, locale: orgLocale, timezone: orgTimezone, week_start: orgWeekStart, product_updates: productUpdates.value.opted_in, ...general.value }
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
            // Awaited: $setLocale fetches the locale's catalogue chunk before it
            // applies and persists the choice, and `window.location.reload()`
            // below would otherwise race that write.
            const setLocale = (useNuxtApp() as any).$setLocale as ((c: string) => Promise<void>) | undefined
            if (resolved && typeof setLocale === 'function') await setLocale(resolved)
            initialLocale.value = form.value.locale
        }

        // 5) Save org timezone override (empty string clears it to UTC).
        if (form.value.timezone !== initialTimezone.value) {
            const tzBody = JSON.stringify({ timezone: form.value.timezone || null })
            const tzResp = await useMyFetch('/api/organization/timezone', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: tzBody })
            if (tzResp.status.value !== 'success') throw new Error(tzResp.error?.value?.data?.detail || t('settings.timezone.saveError'))
            initialTimezone.value = form.value.timezone
        }

        // 6) Save org work-week start (empty string clears it to auto/locale).
        if (form.value.week_start !== initialWeekStart.value) {
            const weekBody = JSON.stringify({ week_start: form.value.week_start || null })
            const weekResp = await useMyFetch('/api/organization/week_start', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: weekBody })
            if (weekResp.status.value !== 'success') throw new Error(weekResp.error?.value?.data?.detail || t('settings.weekStart.saveError'))
            effectiveWeekStart.value = (weekResp.data?.value as any)?.effective_week_start ?? effectiveWeekStart.value
            initialWeekStart.value = form.value.week_start
        }

        // 7) Save the product-updates opt-in (instance-wide) only when changed.
        if (productUpdates.value.available && form.value.product_updates !== productUpdates.value.opted_in) {
            const updResp = await useMyFetch('/api/product-updates', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ opted_in: form.value.product_updates, source: 'settings' }) })
            if (updResp.status.value !== 'success') throw new Error(updResp.error?.value?.data?.detail || t('settings.failedToUpdate'))
            productUpdates.value = updResp.data.value as ProductUpdatesStatus
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

