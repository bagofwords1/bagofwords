<template>
    <!-- Settings → Embed: which host applications (Settings → Integrations)
         may show this artifact, and the snippet each one pastes. Embedding
         never widens who may see it: the host's user needs view access too. -->
    <div class="space-y-6" data-testid="settings-embed">
        <div v-if="loading" class="flex justify-center py-12"><Spinner class="w-5 h-5 text-gray-400" /></div>
        <template v-else-if="state">
            <p class="text-xs leading-5 text-gray-500 dark:text-gray-400">{{ tm('embed.tab.intro', mode) }}</p>
            <div v-if="state.visibility === 'none'" class="flex gap-2 text-xs leading-5 text-amber-700 dark:text-amber-300"
                 data-testid="embed-private-warning">
                <UIcon name="i-heroicons-exclamation-circle" class="mt-0.5 h-4 w-4 shrink-0" />
                <span>{{ tm('embed.tab.privateWarning', mode) }}</span>
            </div>

            <section>
                <h3 class="text-sm font-medium text-gray-900 dark:text-white">{{ $t('embed.tab.embeddableIn') }}</h3>
                <div v-if="state.hosts.length === 0" class="mt-2 rounded-lg border border-dashed border-gray-300 px-4 py-6 text-center text-xs leading-5 text-gray-500 dark:border-gray-700 dark:text-gray-400">
                    {{ state.can_manage_hosts ? $t('embed.tab.noHostsAdmin') : $t('embed.tab.noHosts') }}
                </div>
                <ul v-else class="mt-2 space-y-1">
                    <li v-for="host in state.hosts" :key="host.id"
                        class="flex items-center gap-3 rounded-lg px-2.5 py-2 transition-colors"
                        :class="host.allowed && host.id === selected ? 'bg-gray-50 dark:bg-gray-800/60' : 'hover:bg-gray-50 dark:hover:bg-gray-800/40'">
                        <UCheckbox :model-value="host.allowed" :disabled="saving" :data-testid="`embed-host-${host.public_id}`"
                                   @update:model-value="toggle(host, $event)" />
                        <div class="min-w-0 flex-1">
                            <div class="flex items-center gap-1.5">
                                <span class="truncate text-sm text-gray-900 dark:text-white">{{ host.name }}</span>
                                <span v-if="!host.enabled" class="text-2xs text-gray-400">· {{ $t('hostApps.disabled') }}</span>
                            </div>
                            <p class="truncate text-xs text-gray-500 dark:text-gray-400" dir="ltr">{{ host.allowed_origins.join(', ') }}</p>
                        </div>
                        <button v-if="host.allowed" type="button" class="text-2xs text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white"
                                @click="selected = host.id">{{ $t('embed.tab.snippet') }}</button>
                    </li>
                </ul>
                <div class="mt-3 flex flex-wrap items-center gap-2">
                    <UButton v-if="state.can_manage_hosts" size="xs" color="gray" variant="ghost" icon="i-heroicons-cog-6-tooth"
                             :to="applicationSettings" target="_blank" data-testid="embed-manage-applications">{{ $t('connectedApps.manage') }}</UButton>
                    <UButton size="xs" color="gray" variant="ghost" icon="i-heroicons-arrow-path" :loading="loading" @click="load">{{ $t('connectedApps.refresh') }}</UButton>
                </div>
                <p class="mt-2 text-xs text-ink-muted">{{ $t('connectedApps.embedHelp') }}</p>
            </section>

            <section v-if="selectedHost" data-testid="embed-snippets" class="space-y-2">
                <div class="flex items-center justify-between gap-3">
                    <h3 class="text-sm font-medium text-gray-900 dark:text-white">{{ $t('embed.tab.snippetFor', { name: selectedHost.name }) }}</h3>
                    <div class="flex rounded-md bg-gray-100 p-0.5 dark:bg-gray-800">
                        <button v-for="v in variants" :key="v.key" type="button" :data-testid="`embed-variant-${v.key}`"
                                class="rounded px-2.5 py-0.5 text-2xs transition-colors"
                                :class="variant === v.key ? 'bg-white text-gray-900 shadow-sm dark:bg-gray-900 dark:text-white' : 'text-gray-500 hover:text-gray-800 dark:text-gray-400'"
                                @click="variant = v.key">{{ v.label }}</button>
                    </div>
                </div>
                <EmbedCode :code="snippet" testid="embed-snippet"
                           :langs="variant === 'backend' ? EXAMPLE_LANGS : undefined" v-model="lang" />
                <p class="text-xs leading-5 text-gray-500 dark:text-gray-400">{{ modeNote }}</p>
            </section>
        </template>
    </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import EmbedCode from '~/components/embed/EmbedCode.vue'
import { EXAMPLE_LANGS, backendExample, type ExampleLang } from '~/utils/embedExamples'

const props = defineProps<{ dashboardId: string; slug?: string | null; mode?: string | null; active: boolean }>()
const { t } = useI18n()
const { tm } = useArtifactWording()
const toast = useToast()
const { getErrorMessage } = useErrorMessage()

const state = ref<any>(null)
const loading = ref(false)
const saving = ref(false)
const selected = ref<string | null>(null)
const variant = ref<'inline' | 'floating' | 'backend'>('inline')
const lang = ref<ExampleLang>('python')
const variants = computed(() => [
    { key: 'inline' as const, label: t('embed.tab.inline') },
    { key: 'floating' as const, label: t('embed.tab.floating') },
    { key: 'backend' as const, label: t('embed.tab.backend') },
])

const selectedHost = computed(() => state.value?.hosts.find((h: any) => h.id === selected.value && h.allowed) || null)

async function load() {
    loading.value = !state.value
    try {
        const { data, error } = await useMyFetch(`/api/artifacts/${props.dashboardId}/embedding`)
        if (error.value) { toast.add({ title: getErrorMessage(error.value), color: 'red' }); return }
        state.value = data.value
        if (!selectedHost.value) selected.value = state.value.hosts.find((h: any) => h.allowed)?.id || null
    } finally {
        loading.value = false
    }
}

async function put(ids: string[]) {
    saving.value = true
    try {
        const { data, error } = await useMyFetch(`/api/artifacts/${props.dashboardId}/embedding`, {
            method: 'PUT', body: { host_application_ids: ids },
        })
        if (error.value) { toast.add({ title: getErrorMessage(error.value), color: 'red' }); return }
        state.value = data.value
    } finally {
        saving.value = false
    }
}

function toggle(host: any, allowed: boolean) {
    const ids = state.value.hosts.filter((h: any) => (h.id === host.id ? allowed : h.allowed)).map((h: any) => h.id)
    if (allowed) selected.value = host.id
    put(ids)
}

const applicationSettings = computed(() => ({ path: '/settings/integrations', query: {
    open: 'connected_apps', ...(selected.value ? { application: selected.value } : {}),
} }))

watch(() => props.active, (on) => { if (on) load() }, { immediate: true })

const origin = typeof window !== 'undefined' ? window.location.origin : ''
const artifactRef = computed(() => props.slug || props.dashboardId)

const identityLine = computed(() => {
    switch (selectedHost.value?.identity_mode) {
        case 'idp_token':
            return `    identity: { getIdToken: () => msal.acquireTokenSilent({ scopes: ['openid'] }).then(r => r.idToken) },`
        case 'sso':
            return `    // SSO mode: the frame offers ${t('embed.tab.ssoSignIn')}; nothing to configure here.`
        default:
            return `    identity: {
      getAssertion: () => fetch('/bow/assertion', { method: 'POST' })
        .then(r => r.json()).then(b => b.assertion),
    },`
    }
})

const snippet = computed(() => {
    const host = selectedHost.value
    if (!host) return ''
    if (variant.value === 'backend') return backendSnippet(host)
    const placement = variant.value === 'inline' ? `    container: '#bow-${artifactRef.value}',` : `    mode: 'floating',`
    const holder = variant.value === 'inline' ? `<div id="bow-${artifactRef.value}" style="height: 640px"></div>\n` : ''
    return `${holder}<script src="${origin}/api/embed/sdk/v1/bow-embed.js"><\/script>
<script>
  BOWEmbed.mount({
    baseUrl: '${origin}',
    hostApp: '${host.public_id}',
    artifact: '${artifactRef.value}',
${placement}
${identityLine.value}
  })
<\/script>`
})

function backendSnippet(host: any) {
    return backendExample(host, lang.value)
}

const modeNote = computed(() => ({
    assertion: t('embed.tab.noteAssertion'),
    idp_token: t('embed.tab.noteIdp'),
    sso: t('embed.tab.noteSso'),
} as Record<string, string>)[selectedHost.value?.identity_mode] || '')

</script>
