<template>
    <div>
        <SettingsLoading v-if="loading" />

        <UAlert v-if="error" color="red" variant="soft" icon="i-heroicons-exclamation-triangle" :title="error" />

        <div v-if="!loading && !error">
            <SettingsSection :title="$t('settings.generalSections.workspace')" :description="$t('settings.generalSections.workspaceHint')">
                <SettingsRow :label="$t('settings.organizationName')" stacked for-id="org-name">
                    <UInput id="org-name" v-model="form.organization_name" :maxlength="80" :placeholder="$t('settings.workspacePlaceholder')" data-testid="org-name-input" @blur="saveOrganizationName" @keydown.enter.prevent="blurTarget" />
                </SettingsRow>
                <SettingsRow :label="$t('settings.organizationIcon')" :description="$t('settings.iconConstraints')" stacked>
                    <div class="flex items-center gap-4">
                        <div class="w-16 h-16 rounded-lg border border-line bg-surface-subtle overflow-hidden flex items-center justify-center shrink-0">
                            <img v-if="form.icon_url" :src="form.icon_url" alt="" class="max-w-full max-h-full object-contain" />
                            <UIcon v-else name="i-heroicons-building-office" class="w-6 h-6 text-ink-subtle" />
                        </div>
                        <div class="flex items-center gap-2">
                            <UButton size="sm" color="gray" variant="solid" icon="i-heroicons-arrow-up-tray" @click="selectIcon">{{ form.icon_url ? $t('settings.changeIcon') : $t('settings.uploadIconButton') }}</UButton>
                            <UButton v-if="form.icon_url" size="sm" color="red" variant="ghost" @click="removeIcon">{{ $t('common.remove') }}</UButton>
                            <input ref="fileInput" type="file" accept="image/*" class="hidden" @change="onIconSelected" />
                        </div>
                    </div>
                </SettingsRow>
            </SettingsSection>

            <SettingsSection :title="$t('settings.generalSections.assistant')" :description="$t('settings.generalSections.assistantHint')">
                <SettingsRow :label="$t('settings.aiAnalystName')" stacked for-id="ai-name">
                    <UInput id="ai-name" v-text-dir v-model="form.ai_analyst_name" :maxlength="50" placeholder="AI Analyst" data-testid="ai-name-input" @blur="saveGeneral('ai_analyst_name')" @keydown.enter.prevent="blurTarget" />
                </SettingsRow>
                <SettingsRow :label="$t('settings.showCredit')">
                    <UToggle v-model="form.bow_credit" :aria-label="$t('settings.showCredit')" data-testid="bow-credit-toggle" @change="saveGeneral('bow_credit')" />
                </SettingsRow>
            </SettingsSection>

            <SettingsSection :title="$t('settings.generalSections.region')" :description="$t('settings.generalSections.regionHint')">
                <SettingsRow :label="$t('settings.language.label')" :description="$t('settings.language.description')" stacked>
                    <USelect v-model="form.locale" :options="localeOptions" option-attribute="label" value-attribute="value" data-testid="locale-select" @change="saveLocale" />
                </SettingsRow>
                <SettingsRow :label="$t('settings.timezone.label')" :description="$t('settings.timezone.description')" stacked>
                    <USelect v-model="form.timezone" :options="timezoneOptions" option-attribute="label" value-attribute="value" searchable data-testid="timezone-select" @change="saveTimezone" />
                </SettingsRow>
                <SettingsRow :label="$t('settings.weekStart.label')" :description="$t('settings.weekStart.description')" stacked>
                    <USelect v-model="form.week_start" :options="weekStartOptions" option-attribute="label" value-attribute="value" data-testid="week-start-select" @change="saveWeekStart" />
                </SettingsRow>
            </SettingsSection>

            <SettingsSection v-if="frameAncestors.loaded" :title="$t('settings.frameAncestors.title')" :description="$t('settings.frameAncestors.description')">
                <FrameAncestorsEditor
                    v-model="form.frame_ancestors"
                    :can-edit="frameAncestors.can_edit"
                    :server-error="frameAncestorsError"
                    @update:model-value="saveFrameAncestors"
                />
            </SettingsSection>

            <SettingsSection v-if="productUpdates.available" :title="$t('productUpdates.settingsTitle')">
                <SettingsRow
                    :label="$t('productUpdates.toggleLabel')"
                    :description="form.product_updates
                        ? $t('productUpdates.sentTo', { email: productUpdates.email || productUpdates.current_user_email })
                        : undefined"
                >
                    <UToggle v-model="form.product_updates" data-testid="product-updates-toggle" @change="saveProductUpdates" />
                </SettingsRow>
            </SettingsSection>
        </div>
    </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useToast } from '#imports'
import SettingsSection from '~/components/settings/SettingsSection.vue'
import SettingsRow from '~/components/settings/SettingsRow.vue'
import SettingsLoading from '~/components/settings/SettingsLoading.vue'
import FrameAncestorsEditor from '~/components/settings/FrameAncestorsEditor.vue'

const { t } = useI18n()
const { getErrorMessage } = useErrorMessage()

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

// Every field saves on its own when it changes (toggles, selects, the site
// list) or when it loses focus (text). `saved` holds the last value the
// server accepted: a field whose value already matches it sends nothing, and
// a refused save puts the field back to it.
interface Fields {
    organization_name: string
    ai_analyst_name: string
    bow_credit: boolean
    locale: string        // '' === no org override (system default)
    timezone: string      // '' === no override (UTC)
    week_start: string    // '' === auto (derive from locale)
    product_updates: boolean
    frame_ancestors: string[]
}

const loading = ref(true)
const error = ref('')
const form = ref<Fields & { icon_url?: string | null }>({
    organization_name: '',
    ai_analyst_name: 'AI Analyst',
    bow_credit: true,
    locale: '',
    timezone: '',
    week_start: '',
    product_updates: false,
    frame_ancestors: [],
    icon_url: null,
})
const saved = ref<Fields>({ ...form.value })
// Sites allowed to show BOW in an iframe. Instance-wide like the license:
// read-only for an org admin on a multi-organization instance.
interface FrameAncestorsStatus {
    origins: string[]
    can_edit: boolean
}
const frameAncestors = ref<FrameAncestorsStatus & { loaded: boolean }>({ origins: [], can_edit: false, loaded: false })
// The server's refusal of the site list, shown under the list itself.
const frameAncestorsError = ref('')
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
const enabledLocales = ref<string[]>([])
const systemDefaultLocale = ref<string>('en')
const supportedTimezones = ref<string[]>([])
// What the AI actually uses when week start is on auto.
const effectiveWeekStart = ref<string>('monday')
const fileInput = ref<HTMLInputElement | null>(null)
const toast = useToast()
const { organization } = useOrganization()
const { getSession } = useAuth()
const { settings: orgSettings } = useOrgSettings()

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
        const framingResp = await useMyFetch('/api/settings/frame-ancestors')
        if (framingResp.status.value === 'success' && framingResp.data.value) {
            frameAncestors.value = { ...(framingResp.data.value as FrameAncestorsStatus), loaded: true }
        }
        if (settingsResp.status.value !== 'success') throw new Error(settingsResp.error?.value?.data?.message || t('settings.failedToFetch'))
        const general: GeneralConfig = (settingsResp.data.value as SettingsResponse)?.config?.general || { ai_analyst_name: 'AI Analyst', bow_credit: true }

        const loc = localeResp.data.value as LocaleResponse | null
        enabledLocales.value = loc?.enabled_locales ?? ['en']
        systemDefaultLocale.value = loc?.default_locale ?? 'en'

        supportedTimezones.value = (tzListResp.data.value as { timezones?: string[] } | null)?.timezones ?? []

        const week = weekResp.data.value as { org_week_start?: string | null; effective_week_start?: string } | null
        effectiveWeekStart.value = week?.effective_week_start ?? 'monday'

        const fields: Fields = {
            organization_name: organization.value?.name || '',
            ai_analyst_name: general.ai_analyst_name,
            bow_credit: general.bow_credit,
            locale: loc?.org_locale ?? '',
            timezone: (tzResp.data.value as { org_timezone?: string | null } | null)?.org_timezone ?? '',
            week_start: week?.org_week_start ?? '',
            product_updates: productUpdates.value.opted_in,
            frame_ancestors: [...frameAncestors.value.origins],
        }
        saved.value = { ...fields, frame_ancestors: [...fields.frame_ancestors] }
        form.value = { ...fields, icon_url: general.icon_url || null }
    } catch (e: any) {
        error.value = e.message || t('settings.failedToLoad')
        toast.add({ title: t('common.error'), description: error.value, color: 'red' })
    } finally {
        loading.value = false
    }
}

// One save at a time. The general fields, locale, timezone and week start
// all live in the same settings row, so two requests in flight at once could
// each write back the other's old value.
let queue: Promise<unknown> = Promise.resolve()
const enqueue = (task: () => Promise<void>): Promise<void> => {
    const next = queue.then(task, task)
    queue = next.catch(() => {})
    return next
}

const send = async (url: string, method: 'PUT' | 'POST' | 'DELETE', body?: unknown, fallback = t('settings.failedToUpdate')) => {
    const resp = await useMyFetch(url, {
        method,
        ...(body instanceof FormData
            ? { body }
            : body !== undefined ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}),
    })
    if (resp.status.value !== 'success') throw new Error(getErrorMessage(resp.error.value, fallback))
    return resp.data.value as any
}

const sameValue = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)
const copy = <T>(v: T): T => (Array.isArray(v) ? ([...v] as T) : v)

const notifySaved = () => toast.add({ title: t('settings.saved'), color: 'green', timeout: 2000 })
const notifyFailed = (message: string) => toast.add({ title: t('common.error'), description: message, color: 'red', timeout: 5000 })

// Saves one field. The value is read when its turn comes, so two quick
// changes to the same field each save in order and the last one wins.
// `request` returns the value the server ended up storing, when it differs.
const persist = <K extends keyof Fields>(
    field: K,
    request: (value: Fields[K]) => Promise<Fields[K] | void>,
    fallback: string,
    onError?: (message: string) => void,
) => enqueue(async () => {
    const value = copy(form.value[field])
    if (sameValue(value, saved.value[field])) return
    try {
        const stored = await request(value)
        const result = (stored ?? value) as Fields[K]
        saved.value[field] = copy(result)
        // Only replace what the admin sees when they haven't moved on since.
        if (sameValue(form.value[field], value)) form.value[field] = copy(result)
        notifySaved()
    } catch (e: any) {
        form.value[field] = copy(saved.value[field])
        const message = e?.message || fallback
        if (onError) onError(message)
        else notifyFailed(message)
    }
})

const blurTarget = (e: KeyboardEvent) => (e.target as HTMLElement | null)?.blur()

// Keeps the cached settings other pages read in step with what was just
// saved.
const syncOrgSettingsCache = (data: SettingsResponse | undefined) => {
    const general = data?.config?.general
    if (general && orgSettings.value?.config) orgSettings.value.config.general = { ...general }
}

const saveOrganizationName = () => {
    const name = (form.value.organization_name || '').trim()
    if (!name) {
        form.value.organization_name = saved.value.organization_name
        notifyFailed(t('settings.organizationNameRequired'))
        return
    }
    form.value.organization_name = name
    return persist('organization_name', async (value) => {
        const data = await send('/api/organization', 'PUT', { name: value })
        // The sidebar header reads the org name from here; the org switcher
        // reads it from the session.
        organization.value.name = data?.name || value
        getSession({ force: true }).catch(() => {})
        return data?.name
    }, t('settings.failedToUpdate'))
}

type GeneralField = 'ai_analyst_name' | 'bow_credit'
const saveGeneral = (field: GeneralField) => {
    if (field === 'ai_analyst_name') {
        const name = (form.value.ai_analyst_name || '').trim()
        if (!name) {
            form.value.ai_analyst_name = saved.value.ai_analyst_name
            notifyFailed(t('settings.aiAnalystNameRequired'))
            return
        }
        form.value.ai_analyst_name = name
    }
    // Only the changed key: the server merges it over the stored settings.
    return persist(field, async (value) => {
        const data = await send('/api/organization/settings', 'PUT', { config: { general: { [field]: value } } }) as SettingsResponse
        syncOrgSettingsCache(data)
        return data?.config?.general?.[field] as any
    }, t('settings.failedToUpdate'))
}

const saveLocale = () => persist('locale', async (value) => {
    const data = await send('/api/organization/locale', 'PUT', { locale: value || null }, t('settings.language.saveError'))
    // Switch the admin's own view right away.
    const resolved = data?.effective_locale as string | undefined
    const setLocale = (useNuxtApp() as any).$setLocale as ((c: string) => Promise<void>) | undefined
    if (resolved && typeof setLocale === 'function') await setLocale(resolved)
    // On auto, the week start follows the locale.
    const week = await useMyFetch('/api/organization/week_start')
    const effective = (week.data.value as { effective_week_start?: string } | null)?.effective_week_start
    if (effective) effectiveWeekStart.value = effective
}, t('settings.language.saveError'))

const saveTimezone = () => persist('timezone', async (value) => {
    await send('/api/organization/timezone', 'PUT', { timezone: value || null }, t('settings.timezone.saveError'))
}, t('settings.timezone.saveError'))

const saveWeekStart = () => persist('week_start', async (value) => {
    const data = await send('/api/organization/week_start', 'PUT', { week_start: value || null }, t('settings.weekStart.saveError'))
    effectiveWeekStart.value = data?.effective_week_start ?? effectiveWeekStart.value
}, t('settings.weekStart.saveError'))

const saveProductUpdates = () => persist('product_updates', async (value) => {
    productUpdates.value = await send('/api/product-updates', 'PUT', { opted_in: value, source: 'settings' }) as ProductUpdatesStatus
    return productUpdates.value.opted_in
}, t('settings.failedToUpdate'))

// The editor checks each address before adding it; the server checks the
// whole list again. A refusal shows under the list and puts it back.
const saveFrameAncestors = (origins: string[]) => {
    if (!frameAncestors.value.can_edit) return
    form.value.frame_ancestors = [...origins]
    frameAncestorsError.value = ''
    return persist('frame_ancestors', async (value) => {
        const data = await send('/api/settings/frame-ancestors', 'PUT', { origins: value }) as FrameAncestorsStatus
        frameAncestors.value = { ...data, loaded: true }
        return [...data.origins]
    }, t('settings.failedToUpdate'), (message) => { frameAncestorsError.value = message })
}

// The icon uploads as soon as a file is picked; the preview shows meanwhile
// and goes back to the previous icon if the upload is refused.
let previewUrl: string | null = null
const releasePreview = () => {
    if (previewUrl) URL.revokeObjectURL(previewUrl)
    previewUrl = null
}

const applyIcon = (data: SettingsResponse | undefined) => {
    syncOrgSettingsCache(data)
    // The sidebar and the org switcher read the icon from the session.
    getSession({ force: true }).catch(() => {})
}

const selectIcon = () => fileInput.value?.click()

const onIconSelected = (evt: Event) => {
    const input = evt.target as HTMLInputElement
    const file = input.files?.[0]
    if (fileInput.value) fileInput.value.value = ''
    if (!file) return
    if (file.size > 512 * 1024) {
        toast.add({ title: t('settings.iconTooLarge'), description: t('settings.iconMaxSize'), color: 'red' })
        return
    }
    const previous = form.value.icon_url ?? null
    releasePreview()
    previewUrl = URL.createObjectURL(file)
    form.value.icon_url = previewUrl
    return enqueue(async () => {
        try {
            const formData = new FormData()
            formData.append('icon', file)
            const data = await send('/api/organization/general/icon', 'POST', formData, t('settings.uploadFailed')) as SettingsResponse
            form.value.icon_url = data?.config?.general?.icon_url || null
            applyIcon(data)
            notifySaved()
        } catch (e: any) {
            form.value.icon_url = previous
            notifyFailed(e?.message || t('settings.uploadFailed'))
        } finally {
            releasePreview()
        }
    })
}

const removeIcon = () => {
    const previous = form.value.icon_url ?? null
    form.value.icon_url = null
    return enqueue(async () => {
        try {
            const data = await send('/api/organization/general/icon', 'DELETE', undefined, t('settings.removeFailed')) as SettingsResponse
            applyIcon(data)
            notifySaved()
        } catch (e: any) {
            form.value.icon_url = previous
            notifyFailed(e?.message || t('settings.removeFailed'))
        }
    })
}

onMounted(fetchSettings)
onBeforeUnmount(releasePreview)
</script>
