<template>
	<UModal v-model="isOpen" :ui="{ width: 'sm:max-w-xl' }">
		<UCard :ui="{ body: { padding: 'px-5 py-4 sm:p-5' }, header: { padding: 'px-5 py-3 sm:px-5 sm:py-3' } }">
			<template #header>
				<div class="flex items-center justify-between">
					<h3 class="text-sm font-semibold text-gray-900">Webhooks</h3>
					<UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" size="xs" @click="isOpen = false" />
				</div>
			</template>

			<!-- Existing webhooks -->
			<div v-if="webhooks.length" class="space-y-1.5 mb-4">
				<div
					v-for="w in webhooks"
					:key="w.id"
					class="flex items-center gap-2.5 px-3 py-2 rounded-lg border border-gray-100 bg-white"
				>
					<Icon :name="sourceIcon(w.source)" class="w-4 h-4 flex-shrink-0 text-gray-400" />
					<div class="flex-1 min-w-0">
						<div class="text-sm text-gray-700 truncate">{{ w.name }}</div>
						<div class="text-[11px] text-gray-400">{{ w.source }} · {{ w.auth_mode }}<span v-if="w.classify_enabled"> · AI</span></div>
					</div>
					<UTooltip text="Rotate signing key">
						<button class="text-gray-300 hover:text-gray-600 p-1" @click="rotate(w)"><Icon name="heroicons-arrow-path" class="w-3.5 h-3.5" /></button>
					</UTooltip>
					<UTooltip text="Delete">
						<button class="text-gray-300 hover:text-red-500 p-1" @click="remove(w)"><Icon name="heroicons-trash" class="w-3.5 h-3.5" /></button>
					</UTooltip>
				</div>
			</div>
			<div v-else-if="!loading" class="text-xs text-gray-400 mb-4">No webhooks yet.</div>

			<!-- Secret reveal (shown once after create/rotate) -->
			<div v-if="reveal" class="mb-4 rounded-lg border border-green-100 bg-green-50/50 p-3 space-y-2">
				<div class="text-[11px] font-medium text-green-700">Copy these now — the signing key is shown only once.</div>
				<div class="flex items-center gap-2">
					<span class="text-[10px] uppercase tracking-wide text-gray-400 w-16">URL</span>
					<code class="flex-1 text-[11px] text-gray-700 truncate">{{ reveal.delivery_url }}</code>
					<button class="text-gray-400 hover:text-gray-700" @click="copy(reveal.delivery_url)"><Icon name="heroicons-clipboard" class="w-3.5 h-3.5" /></button>
				</div>
				<div class="flex items-center gap-2">
					<span class="text-[10px] uppercase tracking-wide text-gray-400 w-16">Key</span>
					<code class="flex-1 text-[11px] text-gray-700 truncate">{{ reveal.secret }}</code>
					<button class="text-gray-400 hover:text-gray-700" @click="copy(reveal.secret)"><Icon name="heroicons-clipboard" class="w-3.5 h-3.5" /></button>
				</div>
			</div>

			<!-- New webhook form -->
			<div class="border-t border-gray-100 pt-4 space-y-3">
				<div class="text-xs font-medium text-gray-500">New webhook</div>

				<div>
					<label class="block text-[11px] text-gray-400 mb-1">Name</label>
					<input v-model="form.name" type="text" placeholder="PR triage"
						class="w-full rounded border border-gray-200 px-2.5 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-blue-300" />
				</div>

				<div class="flex gap-3">
					<div class="flex-1">
						<label class="block text-[11px] text-gray-400 mb-1">Source</label>
						<select v-model="form.source" @change="onSourceChange"
							class="w-full rounded border border-gray-200 px-2 py-1.5 text-sm bg-white">
							<option v-for="s in sources" :key="s.source" :value="s.source">{{ s.label }}</option>
						</select>
					</div>
					<div class="flex-1">
						<label class="block text-[11px] text-gray-400 mb-1">Auth</label>
						<select v-model="form.auth_mode"
							class="w-full rounded border border-gray-200 px-2 py-1.5 text-sm bg-white">
							<option value="hmac">HMAC (recommended)</option>
							<option value="token">Token header</option>
							<option value="url_token">URL token</option>
						</select>
					</div>
				</div>
				<p v-if="form.auth_mode !== 'hmac'" class="text-[11px] text-amber-600 -mt-1">
					⚠ {{ form.auth_mode === 'token' ? 'Token in a header — replayable, use for systems that can\'t sign (e.g. Jira Cloud).' : 'Secret lives in the URL — weakest; for systems that can only POST (e.g. Jira Server).' }}
				</p>

				<div>
					<label class="flex items-center gap-2 text-sm text-gray-600 cursor-pointer">
						<input v-model="form.classify_enabled" type="checkbox" class="rounded border-gray-300" />
						Let AI decide whether to respond
					</label>
				</div>

				<div v-if="form.classify_enabled">
					<label class="block text-[11px] text-gray-400 mb-1">Guidance (optional)</label>
					<textarea v-model="form.classifier_prompt" rows="2" placeholder="Only respond to PRs touching billing; ignore dependabot."
						class="w-full rounded border border-gray-200 px-2.5 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-blue-300"></textarea>
				</div>

				<div class="flex justify-end">
					<UButton color="black" size="sm" :loading="creating" @click="create">Create webhook</UButton>
				</div>
			</div>
		</UCard>
	</UModal>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'

const props = defineProps<{ modelValue: boolean; reportId: string }>()
const emit = defineEmits(['update:modelValue', 'changed'])

const isOpen = computed({
	get: () => props.modelValue,
	set: (v: boolean) => emit('update:modelValue', v),
})

const webhooks = ref<any[]>([])
const sources = ref<any[]>([{ source: 'github', label: 'GitHub', default_auth_mode: 'hmac' }, { source: 'jira', label: 'Jira', default_auth_mode: 'token' }, { source: 'generic', label: 'Generic', default_auth_mode: 'hmac' }])
const loading = ref(false)
const creating = ref(false)
const reveal = ref<any>(null)

const form = ref({ name: 'Webhook', source: 'github', auth_mode: 'hmac', classify_enabled: true, classifier_prompt: '' })

function sourceIcon(s: string): string {
	switch ((s || '').toLowerCase()) {
		case 'github': return 'heroicons-code-bracket-square'
		case 'jira': return 'heroicons-bug-ant'
		default: return 'heroicons-bolt'
	}
}

function onSourceChange() {
	const s = sources.value.find(x => x.source === form.value.source)
	if (s?.default_auth_mode) form.value.auth_mode = s.default_auth_mode
}

async function loadSources() {
	try {
		const { data } = await useMyFetch('/webhooks/sources')
		if (Array.isArray(data.value)) sources.value = data.value as any[]
	} catch {}
}

async function loadWebhooks() {
	loading.value = true
	try {
		const { data } = await useMyFetch(`/reports/${props.reportId}/webhooks`)
		webhooks.value = (data.value as any[]) || []
	} catch { webhooks.value = [] } finally { loading.value = false }
}

async function create() {
	creating.value = true
	try {
		const { data, error } = await useMyFetch(`/reports/${props.reportId}/webhooks`, {
			method: 'POST', body: { ...form.value },
		})
		if (error.value) throw error.value
		reveal.value = data.value
		form.value = { name: 'Webhook', source: 'github', auth_mode: 'hmac', classify_enabled: true, classifier_prompt: '' }
		await loadWebhooks()
		emit('changed')
	} catch (e) { console.error('create webhook failed', e) } finally { creating.value = false }
}

async function rotate(w: any) {
	const { data } = await useMyFetch(`/reports/${props.reportId}/webhooks/${w.id}/rotate`, { method: 'POST' })
	if (data.value) reveal.value = data.value
}

async function remove(w: any) {
	await useMyFetch(`/reports/${props.reportId}/webhooks/${w.id}`, { method: 'DELETE' })
	await loadWebhooks()
	emit('changed')
}

function copy(text: string) {
	if (text) navigator.clipboard.writeText(text)
}

watch(isOpen, (open) => {
	if (open) {
		reveal.value = null
		loadSources()
		loadWebhooks()
	}
})
</script>
