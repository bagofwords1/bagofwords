<template>
    <div data-testid="sandbox-page">
        <!-- Sub-page navigation (Sandboxes, Computes, Environments, Settings) lives in the
             admin sidebar under "Sandbox"; the selected one is ?tab=<key>.
             Nothing renders until the deployment flag is known to be on. -->
        <template v-if="featureEnabled">
            <SandboxSandboxes v-if="activeTab === 'sandboxes'" />
            <SandboxComputes v-else-if="activeTab === 'computes'" />
            <SandboxEnvironments v-else-if="activeTab === 'environments'" />
            <SandboxSettings v-else-if="activeTab === 'settings'" />
        </template>
        <SettingsLoading v-else />
    </div>
</template>

<script setup lang="ts">
import { useAdminNav } from '~/composables/useAdminNav'
import { useAppSettings } from '~/composables/useAppSettings'
import SettingsLoading from '~/components/settings/SettingsLoading.vue'
import SandboxSandboxes from '~/components/sandbox/SandboxSandboxes.vue'
import SandboxComputes from '~/components/sandbox/SandboxComputes.vue'
import SandboxEnvironments from '~/components/sandbox/SandboxEnvironments.vue'
import SandboxSettings from '~/components/sandbox/SandboxSettings.vue'

const route = useRoute()
const { visibleItems } = useAdminNav()
const { settings: appSettings, loading: appSettingsLoading, fetchSettings } = useAppSettings()

// The feature exists only when the deployment turns it on. Opened by URL with
// the flag off, the page leaves for the admin landing page instead of showing
// a section whose every request would answer agent_sandboxes.disabled.
const featureEnabled = computed(() => appSettings.value?.agent_sandboxes?.enabled === true)

async function leaveIfDisabled() {
    if (!appSettings.value && !appSettingsLoading.value) await fetchSettings()
    if (appSettingsLoading.value) return // the watcher below runs again when it lands
    if (!featureEnabled.value) await navigateTo('/settings', { replace: true })
}
watch([appSettings, appSettingsLoading], leaveIfDisabled)
onMounted(leaveIfDisabled)

const visibleTabs = computed(() =>
    visibleItems.value.find(item => item.name === 'sandbox')?.children || []
)
const activeTab = computed(() => {
    const requested = String(route.query.tab || '')
    const tabs = visibleTabs.value
    return tabs.find(tab => tab.key === requested)?.key || tabs[0]?.key || 'sandboxes'
})

definePageMeta({
    permissions: ['manage_sandboxes'],
})
</script>
