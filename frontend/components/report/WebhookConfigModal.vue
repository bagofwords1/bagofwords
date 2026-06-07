<template>
	<UModal v-model="isOpen" :ui="{ width: 'sm:max-w-xl' }">
		<UCard :ui="{ body: { padding: 'px-6 py-5 sm:p-6' }, header: { padding: 'px-6 py-4 sm:px-6 sm:py-4' } }">
			<template #header>
				<div class="flex items-start justify-between">
					<div>
						<h3 class="text-base font-semibold text-gray-900">Webhooks</h3>
						<p class="text-sm text-gray-400 mt-0.5">Send external events into this report.</p>
					</div>
					<UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" size="xs" @click="isOpen = false" />
				</div>
			</template>

			<!-- Existing webhooks -->
			<section v-if="webhooks.length" class="mb-6">
				<h4 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">Active</h4>
				<div class="space-y-1.5">
					<div
						v-for="w in webhooks"
						:key="w.id"
						class="flex items-center gap-3 px-3 py-2.5 rounded-lg border border-gray-200"
					>
						<Icon :name="sourceIcon(w.source)" class="w-4 h-4 flex-shrink-0 text-gray-400" />
						<div class="flex-1 min-w-0">
							<div class="text-sm text-gray-800 truncate">{{ w.name }}</div>
							<div class="text-[11px] text-gray-400">{{ w.source }} · {{ authLabel(w.auth_mode) }}<span v-if="w.classify_enabled"> · AI</span></div>
						</div>
						<UTooltip text="Rotate signing key">
							<button class="text-gray-300 hover:text-gray-600 p-1" @click="rotate(w)"><Icon name="heroicons-arrow-path" class="w-4 h-4" /></button>
						</UTooltip>
						<UTooltip text="Delete">
							<button class="text-gray-300 hover:text-red-500 p-1" @click="remove(w)"><Icon name="heroicons-trash" class="w-4 h-4" /></button>
						</UTooltip>
					</div>
				</div>
			</section>

			<!-- Secret reveal (shown once after create / rotate) -->
			<section v-if="reveal" class="mb-6">
				<h4 class="text-xs font-medium text-green-600 uppercase tracking-wide mb-2">Copy now — shown only once</h4>
				<div class="space-y-2">
					<div class="flex items-center gap-2 rounded-lg border border-gray-200 bg-gray-50 px-3 py-2">
						<span class="text-[10px] font-medium uppercase tracking-wide text-gray-400 w-10">URL</span>
						<code class="flex-1 text-xs text-gray-700 truncate">{{ reveal.delivery_url }}</code>
						<button class="text-gray-400 hover:text-gray-700" @click="copy(reveal.delivery_url)"><Icon name="heroicons-clipboard-document" class="w-4 h-4" /></button>
					</div>
					<div class="flex items-center gap-2 rounded-lg border border-gray-200 bg-gray-50 px-3 py-2">
						<span class="text-[10px] font-medium uppercase tracking-wide text-gray-400 w-10">Key</span>
						<code class="flex-1 text-xs text-gray-700 truncate">{{ reveal.secret }}</code>
						<button class="text-gray-400 hover:text-gray-700" @click="copy(reveal.secret)"><Icon name="heroicons-clipboard-document" class="w-4 h-4" /></button>
					</div>
				</div>
			</section>

			<!-- New webhook -->
			<section>
				<h4 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-3">New webhook</h4>

				<div class="space-y-4">
					<div>
						<label class="block text-xs text-gray-500 mb-1.5">Name</label>
						<input v-model="form.name" type="text" placeholder="PR triage"
							class="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm text-gray-800 focus:outline-none focus:ring-2 focus:ring-blue-100 focus:border-blue-300" />
					</div>

					<div class="grid grid-cols-2 gap-3">
						<div>
							<label class="block text-xs text-gray-500 mb-1.5">Source</label>
							<select v-model="form.source"
								class="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm text-gray-800 bg-white focus:outline-none focus:ring-2 focus:ring-blue-100 focus:border-blue-300">
								<option v-for="s in sources" :key="s.source" :value="s.source">{{ s.label }}</option>
							</select>
						</div>
						<div>
							<label class="block text-xs text-gray-500 mb-1.5">Auth</label>
							<select v-model="form.auth_mode"
								class="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm text-gray-800 bg-white focus:outline-none focus:ring-2 focus:ring-blue-100 focus:border-blue-300">
								<option value="token">Token header</option>
								<option value="hmac">HMAC (signed)</option>
								<option value="url_token">URL token</option>
							</select>
						</div>
					</div>

					<label class="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
						<input v-model="form.classify_enabled" type="checkbox" class="rounded border-gray-300 text-blue-600 focus:ring-blue-200" />
						Let AI decide whether to respond
					</label>

					<div v-if="form.classify_enabled">
						<label class="block text-xs text-gray-500 mb-1.5">Guidance (optional)</label>
						<textarea v-model="form.classifier_prompt" rows="2" placeholder="Only respond to PRs touching billing; ignore dependabot."
							class="w-full rounded-lg border border-gray-200 px-3 py-2 text-sm text-gray-800 focus:outline-none focus:ring-2 focus:ring-blue-100 focus:border-blue-300"></textarea>
					</div>

					<div class="flex justify-end pt-1">
						<button
							:disabled="creating"
							class="inline-flex items-center gap-2 rounded-lg bg-gray-900 px-4 py-2 text-sm font-medium text-white hover:bg-gray-800 disabled:opacity-60 disabled:cursor-not-allowed transition-colors"
							@click="create"
						>
							<Spinner v-if="creating" class="w-4 h-4" />
							Create webhook
						</button>
					</div>
				</div>
			</section>
		</UCard>
	</UModal>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import Spinner from '~/components/Spinner.vue'

const props = defineProps<{ modelValue: boolean; reportId: string }>()
const emit = defineEmits(['update:modelValue', 'changed'])

const isOpen = computed({
	get: () => props.modelValue,
	set: (v: boolean) => emit('update:modelValue', v),
})

const webhooks = ref<any[]>([])
const sources = ref<any[]>([
	{ source: 'github', label: 'GitHub' },
	{ source: 'jira', label: 'Jira' },
	{ source: 'generic', label: 'Generic' },
])
const creating = ref(false)
const reveal = ref<any>(null)

// Token header is the default auth mode (simplest for most senders).
const defaultForm = () => ({ name: 'Webhook', source: 'github', auth_mode: 'token', classify_enabled: true, classifier_prompt: '' })
const form = ref(defaultForm())

function sourceIcon(s: string): string {
	switch ((s || '').toLowerCase()) {
		case 'github': return 'heroicons-code-bracket-square'
		case 'jira': return 'heroicons-bug-ant'
		default: return 'heroicons-bolt'
	}
}
function authLabel(a: string): string {
	return a === 'token' ? 'token' : a === 'url_token' ? 'url token' : 'hmac'
}

async function loadSources() {
	try {
		const { data } = await useMyFetch('/webhooks/sources')
		if (Array.isArray(data.value) && data.value.length) sources.value = data.value as any[]
	} catch {}
}

async function loadWebhooks() {
	try {
		const { data } = await useMyFetch(`/reports/${props.reportId}/webhooks`)
		webhooks.value = (data.value as any[]) || []
	} catch { webhooks.value = [] }
}

async function create() {
	creating.value = true
	try {
		const { data, error } = await useMyFetch(`/reports/${props.reportId}/webhooks`, {
			method: 'POST', body: { ...form.value },
		})
		if (error.value) throw error.value
		reveal.value = data.value
		form.value = defaultForm()
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
