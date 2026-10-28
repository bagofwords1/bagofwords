<template>
    <!-- The embedded assistant as the host sees it inside the iframe.
         Floating mode: a launcher when closed, the panel filling the frame
         when open (the SDK resizes the iframe to match, see bow:state).
         Inline mode: the panel always fills the frame. -->
    <div class="h-full w-full flex flex-col items-end justify-end bg-transparent" :style="cssVars" data-testid="embed-root">
        <!-- Launcher (floating, closed) -->
        <button v-if="mode === 'floating' && !open" type="button" @click="$emit('toggle')"
            class="embed-launcher w-14 h-14 rounded-full text-white shadow-lg flex items-center justify-center transition-transform hover:scale-105 m-1"
            :aria-label="launcherLabel" :title="launcherLabel" data-testid="embed-launcher">
            <img v-if="branding.logo_url" :src="branding.logo_url" alt="" class="w-7 h-7 object-contain" />
            <Icon v-else name="heroicons:chat-bubble-oval-left-ellipsis" class="w-7 h-7" />
        </button>

        <!-- Panel -->
        <div v-else class="w-full h-full bg-white dark:bg-gray-900 flex flex-col overflow-hidden"
            :class="mode === 'floating' ? 'rounded-xl border border-gray-200 dark:border-gray-700 shadow-xl' : ''"
            :data-history-loaded="historyLoaded ? 'true' : 'false'"
            data-testid="embed-panel">
            <!-- Header -->
            <div class="flex items-center justify-between px-3 py-2 border-b border-gray-100 dark:border-gray-800 flex-shrink-0 embed-header">
                <div class="flex items-center gap-2 min-w-0">
                    <img v-if="branding.logo_url" :src="branding.logo_url" alt="" class="w-6 h-6 object-contain rounded" />
                    <span v-else class="w-6 h-6 rounded-full flex items-center justify-center text-white embed-accent-bg">
                        <Icon name="heroicons:sparkles" class="w-3.5 h-3.5" />
                    </span>
                    <div class="min-w-0">
                        <div class="text-xs font-semibold text-gray-800 dark:text-gray-100 truncate" data-testid="embed-title">{{ branding.display_name }}</div>
                        <div v-if="userName && authState === 'authenticated'" class="text-2xs text-ink-muted truncate" data-testid="embed-user">{{ userName }}</div>
                    </div>
                </div>
                <div class="flex items-center gap-1">
                    <button v-if="agents.length && allowAgentSelection" type="button"
                        class="text-ink-muted hover:text-gray-700 dark:hover:text-gray-200 p-1 rounded"
                        :title="$t('embed.chooseAgents')" :aria-label="$t('embed.chooseAgents')" @click="showAgents = !showAgents">
                        <Icon name="heroicons:adjustments-horizontal" class="w-4 h-4" />
                    </button>
                    <button v-if="mode === 'floating'" type="button" @click="$emit('close')"
                        class="text-ink-muted hover:text-gray-700 dark:hover:text-gray-200 p-1 rounded"
                        :aria-label="$t('embed.close')" data-testid="embed-close">
                        <Icon name="heroicons:x-mark" class="w-4 h-4" />
                    </button>
                </div>
            </div>

            <!-- Agent picker (only when the embed allows it) -->
            <div v-if="showAgents && allowAgentSelection && agents.length" class="px-3 py-2 border-b border-gray-100 dark:border-gray-800 flex flex-wrap gap-1.5 flex-shrink-0" data-testid="embed-agent-picker">
                <button v-for="a in agents" :key="a.id" type="button"
                    class="px-2 py-0.5 rounded-full text-2xs border"
                    :class="isSelected(a.id) ? 'embed-accent-bg text-white border-transparent' : 'border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-300'"
                    @click="toggleAgent(a.id)">{{ a.name }}</button>
            </div>

            <!-- Auth states -->
            <div v-if="authState === 'disabled'" class="flex-1 flex flex-col items-center justify-center px-6 text-center" data-testid="embed-disabled">
                <Icon name="heroicons:no-symbol" class="w-8 h-8 text-gray-300 mb-3" />
                <p class="text-xs text-gray-500 dark:text-gray-400">{{ $t('embed.disabled') }}</p>
            </div>
            <div v-else-if="authState === 'authenticating' || authState === 'idle'" class="flex-1 flex flex-col items-center justify-center px-6 text-center" data-testid="embed-signing-in">
                <Spinner class="w-6 h-6 text-ink-muted mb-3" />
                <p class="text-xs text-gray-500 dark:text-gray-400">{{ $t('embed.signingIn') }}</p>
            </div>
            <div v-else-if="authState === 'required' || authState === 'failed'" class="flex-1 flex flex-col items-center justify-center px-6 text-center" data-testid="embed-auth-required">
                <Icon name="heroicons:lock-closed" class="w-8 h-8 text-gray-300 mb-3" />
                <p class="text-xs text-gray-500 dark:text-gray-400 mb-1">
                    {{ authState === 'failed' ? $t('embed.authFailed') : $t('embed.signInRequired') }}
                </p>
                <p v-if="authErrorText" class="text-2xs text-ink-muted mb-3" data-testid="embed-auth-error">{{ authErrorText }}</p>
                <button type="button" class="px-3 py-1.5 text-xs font-medium text-white rounded-lg embed-accent-bg" data-testid="embed-retry-auth" @click="$emit('retry-auth')">
                    {{ $t('embed.signInAgain') }}
                </button>
            </div>

            <!-- Thread -->
            <template v-else>
                <CompactChatThread
                    ref="threadEl"
                    :messages="messages"
                    :is-streaming="isStreaming"
                    :accent-color="branding.accent_color"
                    :can-approve="true"
                    :error-text="$t('embed.errorLine')"
                    :stopped-text="$t('embed.stopped')"
                    :approval-text="$t('embed.approvalText')"
                    :allow-text="$t('embed.allow')"
                    :deny-text="$t('embed.deny')"
                    @approve="$emit('approve', $event)">
                    <template #empty>
                        <div class="text-start">
                            <p class="text-sm text-gray-800 dark:text-gray-100 whitespace-pre-wrap" data-testid="embed-greeting">
                                {{ branding.greeting || $t('embed.defaultGreeting', { name: branding.display_name }) }}
                            </p>
                            <div v-if="branding.suggestions?.length" class="mt-3 flex flex-col gap-1.5" data-testid="embed-suggestions">
                                <button v-for="s in branding.suggestions" :key="s" type="button"
                                    class="text-start text-xs px-2.5 py-1.5 rounded-lg border border-gray-200 dark:border-gray-700 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800"
                                    @click="$emit('send', s)">{{ s }}</button>
                            </div>
                        </div>
                    </template>
                </CompactChatThread>

                <!-- Composer -->
                <div class="border-t border-gray-100 dark:border-gray-800 p-2 flex-shrink-0">
                    <CompactChatComposer ref="composerEl"
                        :is-streaming="isStreaming" :can-stop="true"
                        :accent-color="branding.accent_color"
                        :placeholder="$t('embed.placeholder')"
                        :streaming-placeholder="$t('embed.answering')"
                        :send-label="$t('embed.send')" :stop-label="$t('embed.stop')"
                        @send="$emit('send', $event)" @stop="$emit('stop')" />
                    <p v-if="agents.length" class="text-2xs text-gray-300 dark:text-gray-600 mt-1 ps-1 truncate" data-testid="embed-can-query">
                        {{ $t('embed.canQuery', { agents: activeAgentNames }) }}
                    </p>
                </div>
            </template>
        </div>
    </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import Spinner from '~/components/Spinner.vue'
import CompactChatThread from '~/components/chat/CompactChatThread.vue'
import CompactChatComposer from '~/components/chat/CompactChatComposer.vue'
import type { AuthState } from '~/composables/useEmbedSession'

const props = withDefaults(defineProps<{
    branding: Record<string, any>
    mode: 'floating' | 'inline'
    open: boolean
    authState: AuthState
    authError?: string
    userName?: string
    agents: { id: string; name: string }[]
    allowAgentSelection?: boolean
    selectedAgentIds: string[]
    messages: any[]
    isStreaming: boolean
    historyLoaded?: boolean
}>(), { authError: '', userName: '', allowAgentSelection: false, historyLoaded: false })

const emit = defineEmits<{
    (e: 'toggle'): void
    (e: 'close'): void
    (e: 'send', content: string): void
    (e: 'stop'): void
    (e: 'approve', payload: any): void
    (e: 'retry-auth'): void
    (e: 'select-agents', ids: string[]): void
}>()

const { t } = useI18n()
const showAgents = ref(false)
const composerEl = ref<any>(null)
const threadEl = ref<any>(null)

const cssVars = computed(() => ({ '--embed-accent': props.branding.accent_color || '#2563eb' }))
const launcherLabel = computed(() => props.branding.launcher_label || t('embed.launcher', { name: props.branding.display_name }))

const authErrorText = computed(() => {
    const code = props.authError || ''
    if (!code) return ''
    const known: Record<string, string> = {
        'embed.identity_unmapped': t('embed.errors.identityUnmapped'),
        'embed.identity_ambiguous': t('embed.errors.identityAmbiguous'),
        'embed.user_inactive': t('embed.errors.userInactive'),
        'embed.assertion_invalid': t('embed.errors.assertionInvalid'),
        'embed.assertion_replayed': t('embed.errors.assertionInvalid'),
        'embed.session_invalid': t('embed.errors.sessionExpired'),
        'host_timeout': t('embed.errors.hostTimeout'),
    }
    return known[code] || code
})

function isSelected(id: string) {
    return props.selectedAgentIds.length === 0 || props.selectedAgentIds.includes(id)
}

function toggleAgent(id: string) {
    const all = props.agents.map(a => a.id)
    const current = props.selectedAgentIds.length ? [...props.selectedAgentIds] : [...all]
    const next = current.includes(id) ? current.filter(x => x !== id) : [...current, id]
    if (!next.length) return // at least one agent stays selected
    emit('select-agents', next.length === all.length ? [] : next)
}

const activeAgentNames = computed(() => {
    const ids = props.selectedAgentIds.length ? props.selectedAgentIds : props.agents.map(a => a.id)
    return props.agents.filter(a => ids.includes(a.id)).map(a => a.name).join(', ')
})

defineExpose({ focus: () => composerEl.value?.focus?.() })
</script>

<style scoped>
.embed-accent-bg, .embed-launcher { background-color: var(--embed-accent, #2563eb); }
</style>
