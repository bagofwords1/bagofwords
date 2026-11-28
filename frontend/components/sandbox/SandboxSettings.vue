<template>
    <div data-testid="sandbox-settings">
        <SettingsLoading v-if="loading" />

        <p v-else-if="loadError" class="rounded-lg border border-line p-4 text-sm text-red-600 dark:text-red-400" role="alert" data-testid="sandbox-settings-load-error">
            {{ loadError }}
        </p>

        <form v-else @submit.prevent="save">
            <SettingsSection :title="$t('sandbox.settings.enableSection')" :description="$t('sandbox.settings.enableSectionHint')">
                <SettingsRow :label="$t('sandbox.settings.enable')" :description="$t('sandbox.settings.enableHint')">
                    <UToggle v-model="form.enabled" data-testid="sandbox-settings-enabled" />
                </SettingsRow>

                <!-- What is still missing before anyone can use a sandbox. -->
                <div
                    :class="['mt-4 rounded-lg border px-4 py-3 flex items-start gap-3 text-sm', readiness.class]"
                    data-testid="sandbox-settings-readiness"
                    :data-ready="String(readiness.ready)"
                >
                    <UIcon :name="readiness.icon" class="w-4 h-4 shrink-0 mt-0.5" />
                    <div class="min-w-0">
                        <div class="font-medium">{{ readiness.title }}</div>
                        <ul v-if="readiness.missing.length" class="mt-1 text-xs space-y-0.5 list-disc ps-4">
                            <li v-for="item in readiness.missing" :key="item">{{ item }}</li>
                        </ul>
                        <div v-else class="mt-0.5 text-xs opacity-90">{{ readiness.detail }}</div>
                    </div>
                </div>

                <!-- Switching off does not stop what is running. -->
                <div
                    v-if="turningOffWhileRunning"
                    class="mt-3 rounded-lg border border-amber-200 dark:border-amber-900/60 bg-amber-50 dark:bg-amber-950/40 px-4 py-3 flex items-start gap-3 text-sm text-amber-800 dark:text-amber-200"
                    data-testid="sandbox-settings-running-note"
                >
                    <UIcon name="i-heroicons-exclamation-triangle" class="w-4 h-4 shrink-0 mt-0.5" />
                    <span>{{ $t('sandbox.settings.turnOffRunning', { n: state.running_sandboxes }) }}</span>
                </div>
            </SettingsSection>

            <SettingsSection :title="$t('sandbox.settings.defaultsSection')" :description="$t('sandbox.settings.defaultsSectionHint')">
                <SettingsRow :label="$t('sandbox.settings.defaultEnvironment')" :description="$t('sandbox.settings.defaultEnvironmentHint')" stacked for-id="sandbox-settings-default-env">
                    <USelect
                        id="sandbox-settings-default-env"
                        v-model="form.default_environment_id"
                        :options="environmentOptions"
                        option-attribute="label"
                        value-attribute="value"
                        class="sm:max-w-xs"
                        data-testid="sandbox-settings-default-environment"
                    />
                </SettingsRow>
                <SettingsRow :label="$t('sandbox.settings.maxRunningPerUser')" :description="$t('sandbox.settings.maxRunningPerUserHint')" stacked for-id="sandbox-settings-per-user">
                    <UInput
                        id="sandbox-settings-per-user"
                        v-model.number="form.max_running_per_user"
                        type="number"
                        min="1"
                        max="1000"
                        step="1"
                        dir="ltr"
                        class="sm:max-w-[10rem]"
                        data-testid="sandbox-settings-max-per-user"
                    />
                </SettingsRow>
            </SettingsSection>

            <SettingsSection :title="$t('sandbox.settings.lifecycleSection')" :description="$t('sandbox.settings.lifecycleSectionHint')">
                <div class="grid grid-cols-1 sm:grid-cols-3 gap-x-4 gap-y-5">
                    <SettingsRow :label="$t('sandbox.settings.idleTimeout')" :description="$t('sandbox.settings.idleTimeoutHint')" stacked for-id="sandbox-settings-idle">
                        <UInput id="sandbox-settings-idle" v-model.number="form.idle_timeout_minutes" type="number" min="1" max="10080" step="any" dir="ltr" data-testid="sandbox-settings-idle-timeout">
                            <template #trailing><span class="text-xs text-ink-muted">{{ $t('sandbox.settings.minutes') }}</span></template>
                        </UInput>
                    </SettingsRow>
                    <SettingsRow :label="$t('sandbox.settings.stoppedRetention')" :description="$t('sandbox.settings.stoppedRetentionHint')" stacked for-id="sandbox-settings-retention">
                        <UInput id="sandbox-settings-retention" v-model.number="form.stopped_retention_days" type="number" min="0.001" max="365" step="any" dir="ltr" data-testid="sandbox-settings-stopped-retention">
                            <template #trailing><span class="text-xs text-ink-muted">{{ $t('sandbox.settings.days') }}</span></template>
                        </UInput>
                    </SettingsRow>
                    <SettingsRow :label="$t('sandbox.settings.maxLifetime')" :description="$t('sandbox.settings.maxLifetimeHint')" stacked for-id="sandbox-settings-lifetime-mode">
                        <USelect
                            id="sandbox-settings-lifetime-mode"
                            v-model="form.max_lifetime_mode"
                            :options="lifetimeOptions"
                            option-attribute="label"
                            value-attribute="value"
                            data-testid="sandbox-settings-max-lifetime-mode"
                        />
                        <UInput
                            v-if="form.max_lifetime_mode === 'limited'"
                            id="sandbox-settings-lifetime"
                            v-model.number="form.max_lifetime_days"
                            type="number"
                            min="0.004"
                            max="365"
                            step="any"
                            dir="ltr"
                            class="mt-2"
                            :aria-label="$t('sandbox.settings.maxLifetime')"
                            data-testid="sandbox-settings-max-lifetime"
                        >
                            <template #trailing><span class="text-xs text-ink-muted">{{ $t('sandbox.settings.days') }}</span></template>
                        </UInput>
                        <p v-else class="mt-2 text-xs text-ink-muted leading-relaxed" data-testid="sandbox-settings-no-lifetime-note">{{ $t('sandbox.settings.maxLifetimeNoneHint') }}</p>
                    </SettingsRow>
                </div>
                <SettingsRow class="mt-5" :label="$t('sandbox.settings.deleteWarning')" :description="$t('sandbox.settings.deleteWarningHint')" stacked for-id="sandbox-settings-delete-warning">
                    <UInput id="sandbox-settings-delete-warning" v-model.number="form.delete_warning_hours" type="number" min="0" max="720" step="any" dir="ltr" class="sm:max-w-[10rem]" data-testid="sandbox-settings-delete-warning">
                        <template #trailing><span class="text-xs text-ink-muted">{{ $t('sandbox.settings.hours') }}</span></template>
                    </UInput>
                </SettingsRow>
            </SettingsSection>

            <SettingsSection :title="$t('sandbox.settings.snapshotSection')" :description="$t('sandbox.settings.snapshotSectionHint')">
                <ul class="mb-4 space-y-1 text-xs text-ink-muted leading-relaxed list-disc ps-4" data-testid="sandbox-settings-snapshot-explain">
                    <li>{{ $t('sandbox.settings.snapshotWhere') }}</li>
                    <li>{{ $t('sandbox.settings.snapshotCost') }}</li>
                    <li>{{ $t('sandbox.settings.snapshotWhy') }}</li>
                </ul>
                <SettingsRow :label="$t('sandbox.settings.snapshotOnStop')" :description="$t('sandbox.settings.snapshotOnStopHint')">
                    <UToggle v-model="form.snapshot_on_stop" data-testid="sandbox-settings-snapshot-on-stop" />
                </SettingsRow>
                <SettingsRow :label="$t('sandbox.settings.snapshotMax')" :description="$t('sandbox.settings.snapshotMaxHint')" stacked for-id="sandbox-settings-snapshot-max">
                    <UInput id="sandbox-settings-snapshot-max" v-model.number="form.snapshot_max_mb" type="number" min="1" max="1048576" step="1" dir="ltr" class="sm:max-w-[10rem]" data-testid="sandbox-settings-snapshot-max">
                        <template #trailing><span class="text-xs text-ink-muted">MB</span></template>
                    </UInput>
                </SettingsRow>
            </SettingsSection>

            <SettingsSection :title="$t('sandbox.settings.registriesSection')" :description="$t('sandbox.settings.registriesSectionHint')">
                <SettingsRow :label="$t('sandbox.settings.registryDomains')" :description="$t('sandbox.settings.registryDomainsHint')" stacked for-id="sandbox-settings-registries">
                    <UTextarea
                        id="sandbox-settings-registries"
                        v-model="form.registry_domains"
                        :rows="6"
                        dir="ltr"
                        spellcheck="false"
                        placeholder="pypi.org"
                        :ui="{ base: 'font-mono text-xs' }"
                        data-testid="sandbox-settings-registry-domains"
                    />
                </SettingsRow>
            </SettingsSection>

            <p v-if="errorMessage" class="mt-6 text-sm text-red-600 dark:text-red-400 break-words" role="alert" data-testid="sandbox-settings-error">
                {{ errorMessage }}
            </p>

            <SettingsFooter>
                <UButton type="submit" color="blue" :loading="saving" :disabled="!dirty" data-testid="sandbox-settings-save">
                    {{ $t('common.saveChanges') }}
                </UButton>
            </SettingsFooter>
        </form>
    </div>
</template>

<script setup lang="ts">
import type { SandboxEnvironment } from '~/composables/useSandboxApi'
import SettingsSection from '~/components/settings/SettingsSection.vue'
import SettingsRow from '~/components/settings/SettingsRow.vue'
import SettingsFooter from '~/components/settings/SettingsFooter.vue'
import SettingsLoading from '~/components/settings/SettingsLoading.vue'

interface SandboxSettingsState {
    enabled: boolean
    default_environment_id: string | null
    idle_timeout_seconds: number
    stopped_retention_seconds: number
    max_lifetime_seconds: number
    delete_warning_seconds: number
    snapshot_on_stop: boolean
    snapshot_max_mb: number
    registry_domains: string[]
    max_running_per_user: number
    computes_ready: number
    environments: number
    running_sandboxes: number
}

const MINUTE = 60
const HOUR = 3600
const DAY = 24 * 3600
// What "Delete after" starts from when the maximum lifetime was off.
const DEFAULT_LIFETIME_DAYS = 30
const NO_ENVIRONMENT = '__none__'

const { t } = useI18n()
const toast = useToast()
const { call } = useSandboxApi()
const { getErrorMessage } = useErrorMessage()

const loading = ref(true)
const loadError = ref('')
const saving = ref(false)
const errorMessage = ref('')
const state = ref<SandboxSettingsState>({
    enabled: false, default_environment_id: null, idle_timeout_seconds: 0, stopped_retention_seconds: 0,
    max_lifetime_seconds: 0, delete_warning_seconds: 0, snapshot_on_stop: false, snapshot_max_mb: 0,
    registry_domains: [], max_running_per_user: 0,
    computes_ready: 0, environments: 0, running_sandboxes: 0,
})
const environments = ref<SandboxEnvironment[]>([])

// The API stores seconds; the form asks in the unit an admin thinks in.
const form = reactive({
    enabled: false,
    default_environment_id: NO_ENVIRONMENT,
    max_running_per_user: 3 as number | string,
    idle_timeout_minutes: 30 as number | string,
    stopped_retention_days: 7 as number | string,
    // 0 seconds on the server means no maximum lifetime; here it is a choice.
    max_lifetime_mode: 'limited' as 'limited' | 'none',
    max_lifetime_days: 30 as number | string,
    delete_warning_hours: 24 as number | string,
    snapshot_on_stop: false,
    snapshot_max_mb: 2048 as number | string,
    registry_domains: '',
})

const inUnit = (seconds: number, unit: number) => Math.round((seconds / unit) * 1000) / 1000
const parseDomains = (text: string) => text.split(/[\n,]+/).map(domain => domain.trim()).filter(Boolean)

function formFrom(s: SandboxSettingsState) {
    return {
        enabled: !!s.enabled,
        default_environment_id: s.default_environment_id || NO_ENVIRONMENT,
        max_running_per_user: s.max_running_per_user,
        idle_timeout_minutes: inUnit(s.idle_timeout_seconds, MINUTE),
        stopped_retention_days: inUnit(s.stopped_retention_seconds, DAY),
        max_lifetime_mode: (s.max_lifetime_seconds ? 'limited' : 'none') as 'limited' | 'none',
        max_lifetime_days: s.max_lifetime_seconds ? inUnit(s.max_lifetime_seconds, DAY) : DEFAULT_LIFETIME_DAYS,
        delete_warning_hours: inUnit(s.delete_warning_seconds, HOUR),
        snapshot_on_stop: !!s.snapshot_on_stop,
        snapshot_max_mb: s.snapshot_max_mb,
        registry_domains: (s.registry_domains || []).join('\n'),
    }
}

// Only what the admin changed is sent, so a value that does not divide evenly
// into minutes or days is never rewritten by a save that did not touch it.
function changes() {
    const initial = formFrom(state.value)
    const body: Record<string, any> = {}
    if (form.enabled !== initial.enabled) body.enabled = form.enabled
    if (form.default_environment_id !== initial.default_environment_id) {
        body.default_environment_id = form.default_environment_id === NO_ENVIRONMENT ? null : form.default_environment_id
    }
    if (Number(form.max_running_per_user) !== initial.max_running_per_user) body.max_running_per_user = Number(form.max_running_per_user)
    if (Number(form.idle_timeout_minutes) !== initial.idle_timeout_minutes) body.idle_timeout_seconds = Math.round(Number(form.idle_timeout_minutes) * MINUTE)
    if (Number(form.stopped_retention_days) !== initial.stopped_retention_days) body.stopped_retention_seconds = Math.round(Number(form.stopped_retention_days) * DAY)
    if (form.max_lifetime_mode === 'none') {
        if (initial.max_lifetime_mode !== 'none') body.max_lifetime_seconds = 0
    } else if (initial.max_lifetime_mode === 'none' || Number(form.max_lifetime_days) !== initial.max_lifetime_days) {
        body.max_lifetime_seconds = Math.round(Number(form.max_lifetime_days) * DAY)
    }
    if (Number(form.delete_warning_hours) !== initial.delete_warning_hours) body.delete_warning_seconds = Math.round(Number(form.delete_warning_hours) * HOUR)
    if (form.snapshot_on_stop !== initial.snapshot_on_stop) body.snapshot_on_stop = form.snapshot_on_stop
    if (Number(form.snapshot_max_mb) !== initial.snapshot_max_mb) body.snapshot_max_mb = Number(form.snapshot_max_mb)
    const domains = parseDomains(form.registry_domains)
    if (domains.join('\n') !== (state.value.registry_domains || []).join('\n')) body.registry_domains = domains
    return body
}
const dirty = computed(() => Object.keys(changes()).length > 0)

const lifetimeOptions = computed(() => [
    { value: 'limited', label: t('sandbox.settings.maxLifetimeLimited') },
    { value: 'none', label: t('sandbox.settings.maxLifetimeNone') },
])

const environmentOptions = computed(() => [
    { value: NO_ENVIRONMENT, label: t('sandbox.settings.defaultEnvironmentNone') },
    ...environments.value.map(environment => ({
        value: environment.id,
        label: environment.enabled ? environment.name : t('sandbox.settings.environmentDisabled', { name: environment.name }),
    })),
])

const readiness = computed(() => {
    const missing: string[] = []
    if (!state.value.computes_ready) missing.push(t('sandbox.settings.missingCompute'))
    if (!state.value.environments) missing.push(t('sandbox.settings.missingEnvironment'))
    if (missing.length) {
        return {
            ready: false,
            class: 'border-amber-200 dark:border-amber-900/60 bg-amber-50 dark:bg-amber-950/40 text-amber-800 dark:text-amber-200',
            icon: 'i-heroicons-exclamation-triangle',
            title: t('sandbox.settings.missingTitle'),
            missing,
            detail: '',
        }
    }
    return {
        ready: true,
        class: 'border-green-200 dark:border-green-900/60 bg-green-50 dark:bg-green-950/40 text-green-800 dark:text-green-200',
        icon: 'i-heroicons-check-circle',
        title: state.value.enabled ? t('sandbox.settings.readyOn') : t('sandbox.settings.readyOff'),
        missing,
        detail: t('sandbox.settings.readyDetail', {
            computes: state.value.computes_ready,
            environments: state.value.environments,
            running: state.value.running_sandboxes,
        }),
    }
})

const turningOffWhileRunning = computed(() =>
    state.value.enabled && !form.enabled && state.value.running_sandboxes > 0
)

function apply(s: SandboxSettingsState) {
    state.value = s
    Object.assign(form, formFrom(s))
}

async function load() {
    try {
        const [settings, environmentList] = await Promise.all([
            call<SandboxSettingsState>('/settings'),
            call<SandboxEnvironment[]>('/environments'),
        ])
        environments.value = environmentList || []
        apply(settings)
    } catch (error) {
        loadError.value = getErrorMessage(error, t('sandbox.settings.loadFailed'))
    } finally {
        loading.value = false
    }
}

async function save() {
    if (saving.value) return
    saving.value = true
    errorMessage.value = ''
    try {
        apply(await call<SandboxSettingsState>('/settings', { method: 'PUT', body: changes() }))
        toast.add({ title: t('sandbox.settings.saved'), color: 'green' })
    } catch (error) {
        errorMessage.value = getErrorMessage(error, t('sandbox.settings.saveFailed'))
    } finally {
        saving.value = false
    }
}

onMounted(load)
</script>
