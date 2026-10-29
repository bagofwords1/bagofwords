<template>
    <!-- Settings → Embed: which host applications (Settings → Integrations)
         may show this artifact, and the snippet each one pastes. Embedding
         never widens who may see it: the host's user needs view access too. -->
    <div class="space-y-6" data-testid="settings-embed">
        <div v-if="loading" class="flex justify-center py-12"><Spinner class="w-5 h-5 text-gray-400" /></div>
        <template v-else-if="state">
            <p class="text-sm leading-6 text-gray-500 dark:text-gray-400">{{ tm('embed.tab.intro', mode) }}</p>
            <div v-if="state.visibility === 'none'" class="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200"
                 data-testid="embed-private-warning">
                {{ tm('embed.tab.privateWarning', mode) }}
            </div>

            <section>
                <h3 class="text-sm font-medium text-gray-900 dark:text-white">{{ $t('embed.tab.embeddableIn') }}</h3>
                <div v-if="state.hosts.length === 0" class="mt-2 rounded-lg border border-dashed border-gray-300 px-4 py-6 text-center text-xs leading-5 text-gray-500 dark:border-gray-700 dark:text-gray-400">
                    {{ state.can_manage_hosts ? $t('embed.tab.noHostsAdmin') : $t('embed.tab.noHosts') }}
                </div>
                <ul v-else class="mt-2 divide-y divide-gray-100 rounded-lg border border-gray-200 dark:divide-gray-800 dark:border-gray-800">
                    <li v-for="host in state.hosts" :key="host.id" class="flex items-center gap-3 px-3 py-2.5">
                        <UCheckbox :model-value="host.allowed" :disabled="saving" :data-testid="`embed-host-${host.public_id}`"
                                   @update:model-value="toggle(host, $event)" />
                        <div class="min-w-0 flex-1">
                            <div class="flex items-center gap-1.5">
                                <span class="truncate text-sm text-gray-900 dark:text-white">{{ host.name }}</span>
                                <span v-if="!host.enabled" class="text-2xs text-gray-400">· {{ $t('hostApps.disabled') }}</span>
                            </div>
                            <p class="truncate text-xs text-gray-500 dark:text-gray-400" dir="ltr">{{ host.allowed_origins.join(', ') }}</p>
                        </div>
                        <UButton v-if="host.allowed" size="2xs" color="gray" variant="ghost" @click="selected = host.id">
                            {{ $t('embed.tab.snippet') }}
                        </UButton>
                    </li>
                </ul>
                <div v-if="state.can_manage_hosts" class="mt-2">
                    <UButton v-if="!creating" size="xs" color="gray" variant="ghost" icon="i-heroicons-plus" data-testid="embed-new-host"
                             @click="creating = true">{{ $t('embed.tab.newHost') }}</UButton>
                    <div v-else class="mt-2 rounded-lg border border-gray-200 p-4 dark:border-gray-800">
                        <HostApplicationForm @saved="onHostCreated" @cancel="creating = false" />
                    </div>
                </div>
            </section>

            <section v-if="selectedHost" data-testid="embed-snippets">
                <div class="flex items-center justify-between">
                    <h3 class="text-sm font-medium text-gray-900 dark:text-white">{{ $t('embed.tab.snippetFor', { name: selectedHost.name }) }}</h3>
                    <div class="flex gap-1">
                        <UButton v-for="v in variants" :key="v.key" size="2xs" :color="variant === v.key ? 'blue' : 'gray'"
                                 :variant="variant === v.key ? 'soft' : 'ghost'" @click="variant = v.key">{{ v.label }}</UButton>
                    </div>
                </div>
                <div class="relative mt-2" dir="ltr">
                    <pre class="overflow-x-auto rounded-lg bg-gray-950 p-3 text-2xs leading-5 text-gray-100" dir="ltr" data-testid="embed-snippet">{{ snippet }}</pre>
                    <UButton class="absolute right-2 top-2" size="2xs" color="white" icon="i-heroicons-clipboard-document" @click="copy(snippet)">
                        {{ $t('hostApps.copy') }}
                    </UButton>
                </div>
                <p class="mt-2 text-xs leading-5 text-gray-500 dark:text-gray-400">{{ modeNote }}</p>
            </section>
        </template>
    </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import HostApplicationForm from '~/components/embed/HostApplicationForm.vue'

const props = defineProps<{ dashboardId: string; slug?: string | null; mode?: string | null; active: boolean }>()
const { t } = useI18n()
const { tm } = useArtifactWording()
const toast = useToast()
const { getErrorMessage } = useErrorMessage()

const state = ref<any>(null)
const loading = ref(false)
const saving = ref(false)
const creating = ref(false)
const selected = ref<string | null>(null)
const variant = ref<'inline' | 'floating' | 'backend'>('inline')
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

async function onHostCreated(host: any) {
    creating.value = false
    await load()
    toggle(state.value.hosts.find((h: any) => h.id === host.id) || host, true)
}

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
    if (host.identity_mode === 'idp_token') {
        return `// No backend step: BOW verifies the ID token with your identity provider's
// published keys. Request it for the audience configured on "${host.name}".`
    }
    if (host.identity_mode === 'sso') {
        return `// No backend step: the user signs in to BOW once in a popup.`
    }
    return `// POST /bow/assertion — your backend, after checking its own session
import jwt from 'jsonwebtoken'
import crypto from 'node:crypto'

app.post('/bow/assertion', requireLogin, (req, res) => {
  const assertion = jwt.sign(
    { sub: req.user.employeeId },             // what "${host.name}" maps to a BOW member
    process.env.BOW_HOST_PRIVATE_KEY,         // from Settings → Integrations, shown once
    { algorithm: 'RS256', audience: '${host.public_id}', issuer: '<issuer>',
      expiresIn: 60, jwtid: crypto.randomUUID() },
  )
  res.json({ assertion })
})`
}

const modeNote = computed(() => ({
    assertion: t('embed.tab.noteAssertion'),
    idp_token: t('embed.tab.noteIdp'),
    sso: t('embed.tab.noteSso'),
} as Record<string, string>)[selectedHost.value?.identity_mode] || '')

async function copy(value: string) {
    try { await navigator.clipboard.writeText(value); toast.add({ title: t('hostApps.copied') }) } catch { /* no clipboard */ }
}
</script>
