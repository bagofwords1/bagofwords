<!-- What a report with no conversation shows: the training-mode tip sheet, or
     the agent picker and that selection's starter questions. Shared by the
     report view and the draft page (/reports/new) so an empty report and a
     report that doesn't exist yet are the same screen — the draft just has no
     row behind it. The selection itself lives in the prompt box; this emits
     clicks and renders what it is told. -->
<template>
	<div class="mt-32 fade-in">
	<!-- Training mode empty state -->
	<template v-if="mode === 'training'">
		<h1 class="text-4xl mb-4">🎓</h1>
		<h1 class="text-lg font-semibold">{{ $t('reports.trainingEmptyTitle') }}</h1>
		<hr class="my-4">
		<p class="text-gray-500 dark:text-gray-400 text-sm"><span class="font-semibold">{{ $t('reports.trainingEmptyTipLabel') }}</span> <br />
			{{ $t('reports.trainingEmptyBody') }}
		</p>
		<div class="mt-4 flex flex-wrap gap-2">
			<button
				v-for="s in ($tm('reports.trainingStarters') as any[])"
				:key="s.title"
				class="px-3 py-1.5 text-xs rounded-full border border-sky-200 bg-sky-50 text-sky-700 hover:bg-sky-100 transition-colors"
				@click="$emit('starter', `${s.title}\n\n${s.prompt}`)"
			>
				{{ s.title }}
			</button>
		</div>
	</template>
	<!-- Chat / deep mode empty state -->
	<template v-else>
		<div class="flex flex-col items-center text-center">
			<img
				src="/assets/empty-states/empty-integrations.png"
				alt=""
				class="w-56 max-w-full mb-2 select-none pointer-events-none dark:hidden"
			/>
			<div class="hidden dark:flex items-center justify-center w-24 h-24 rounded-2xl bg-gray-800 mb-2">
				<UIcon name="i-heroicons-chat-bubble-left-right" class="w-10 h-10 text-gray-500" />
			</div>
			<h1 class="text-lg font-semibold">{{ $t('reports.emptyTitle') }}</h1>
			<!-- Agent picker + starter questions: one start-aligned column the
			     width of the composer below, so the search rule and the question
			     dividers land on its edges. Both live in a max-w-2xl column, but
			     the message column pads ps-4/pe-2 while the composer card sits a
			     further 16px in on both sides — hence the extra start/end inset
			     here. Below sm the two already line up (px-3 vs p-3). -->
			<div class="w-full text-start sm:ps-4 sm:pe-6">
				<!-- Agents: only worth showing when there's a choice to make.
				     Multi-select — it drives the prompt box's selector, which
				     owns auto-mode and persistence. Most-recently-used first, so
				     the agents you actually work with lead the row. -->
				<div v-if="availableAgents.length > 1" class="mt-7">
					<!-- The search field is the section header: no separate title,
					     it names the row and filters it as you type. -->
					<div class="flex items-center gap-2 px-1 pb-1.5 border-b border-gray-100 dark:border-gray-800">
						<Icon name="heroicons:magnifying-glass" class="w-3.5 h-3.5 flex-shrink-0 text-gray-300 dark:text-gray-600" />
						<input
							v-model="agentChipQuery"
							type="text"
							data-testid="empty-agent-search"
							:placeholder="$t('projects.overview.searchAgents')"
							class="w-full bg-transparent text-[13px] text-gray-700 dark:text-gray-200 placeholder-gray-400 dark:placeholder-gray-500 focus:outline-none"
						/>
					</div>
					<div
						:class="[
							'mt-2 flex flex-wrap gap-1',
							showAllAgentChips ? 'max-h-36 overflow-y-auto' : ''
						]"
					>
						<button
							v-for="a in visibleAgentChips"
							:key="a.id"
							type="button"
							data-testid="empty-agent-chip"
							:aria-pressed="isAgentSelected(a)"
							:class="[
								'inline-flex items-center gap-1.5 px-1.5 py-1 rounded-md text-[13px] transition-colors',
								isAgentSelected(a)
									? 'bg-gray-100 dark:bg-gray-800 text-gray-900 dark:text-gray-100'
									: 'text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800/60'
							]"
							@click="$emit('toggle-agent', a)"
						>
							<DataSourceIcon
								:type="a.type || a.connections?.[0]?.type"
								:connector-key="a.connector_key || a.connections?.[0]?.connector_key"
								:icon="a.icon"
								class="h-3.5 flex-shrink-0"
							/>
							<span class="max-w-[11rem] truncate">{{ a.name }}</span>
							<Icon
								v-if="isAgentSelected(a)"
								name="heroicons:check"
								class="w-3 h-3 flex-shrink-0 text-gray-400"
							/>
						</button>
						<!-- Long agent lists would otherwise bury the questions
						     under a wall of chips — reveal the tail on demand. -->
						<button
							v-if="hiddenAgentChipCount > 0"
							type="button"
							data-testid="empty-agent-chip-more"
							class="inline-flex items-center px-1.5 py-1 rounded-md text-[13px] text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800/60 transition-colors"
							@click="showAllAgentChips = true"
						>
							+{{ hiddenAgentChipCount }}
						</button>
						<span v-if="visibleAgentChips.length === 0" class="px-1.5 py-1 text-[13px] text-gray-400">
							{{ $t('mentionInput.noResults') }}
						</span>
					</div>
					<!-- Outside the scroll area — inside it, collapsing would mean
					     scrolling past every chip to find the way back. -->
					<button
						v-if="showAllAgentChips"
						type="button"
						data-testid="empty-agent-chip-less"
						class="mt-1 px-1.5 text-[12px] text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 transition-colors"
						@click="showAllAgentChips = false"
					>
						{{ $t('tools.common.showLess') }}
					</button>
				</div>
				<!-- Starter questions for the selected agents, one per line.
				     Nothing selected ⇒ nothing to suggest. -->
				<div
					v-if="currentAgents.length > 0 && conversationStarters.length > 0"
					:class="availableAgents.length > 1 ? 'mt-5' : 'mt-7'"
				>
					<ul class="divide-y divide-gray-100 dark:divide-gray-800/70">
						<li v-for="s in conversationStarters" :key="s.title" class="group">
							<button
								type="button"
								dir="auto"
								data-testid="empty-starter"
								class="w-full flex items-center justify-between gap-3 py-2.5 px-1 text-start text-[13px] leading-snug text-gray-500 dark:text-gray-400 transition-colors duration-150 hover:text-gray-900 dark:hover:text-gray-100"
								@click="$emit('starter', `${s.title}\n\n${s.prompt}`)"
							>
								<span class="truncate">{{ s.title }}</span>
								<Icon
									name="heroicons-arrow-up-right"
									class="w-3.5 h-3.5 flex-shrink-0 text-gray-300 dark:text-gray-600 opacity-0 -translate-x-1 group-hover:opacity-100 group-hover:translate-x-0 transition-all duration-150"
								/>
							</button>
						</li>
					</ul>
				</div>
			</div>
		</div>
	</template>
	</div>
</template>

<script setup lang="ts">
import DataSourceIcon from '@/components/DataSourceIcon.vue'

const props = withDefaults(defineProps<{
	// Mode of the composer below, which decides which empty state applies.
	mode?: 'chat' | 'training'
	// Every agent the user could pick, published by the prompt box's selector.
	availableAgents?: any[]
	// The agents currently scoping the conversation.
	currentAgents?: any[]
	// The selection is "Auto" — scoped to everything because the user hasn't
	// chosen. That is the absence of a choice, so no chip reads as selected;
	// the first click is what turns it into a real selection.
	agentsAreAuto?: boolean
}>(), {
	mode: 'chat',
	availableAgents: () => [],
	currentAgents: () => [],
	agentsAreAuto: false,
})

defineEmits<{
	(e: 'toggle-agent', agent: any): void
	(e: 'starter', text: string): void
}>()

// Orgs can have dozens of agents; show a handful and keep the rest one click
// away so the starter questions stay above the fold. Beyond this the row wraps
// past two lines and stops reading as a shortcut — the search field is the way
// through a long roster.
const AGENT_CHIP_LIMIT = 6
const showAllAgentChips = ref(false)
const agentChipQuery = ref('')

// Most-recently-used first (`last_used_at` from /data_sources/active — the last
// conversation this user actually had with the agent), never-used ones after,
// alphabetical within each group so the order is stable and predictable.
const sortedAgents = computed(() => {
	return [...(props.availableAgents || [])].sort((a: any, b: any) => {
		const ta = a?.last_used_at ? Date.parse(a.last_used_at) : 0
		const tb = b?.last_used_at ? Date.parse(b.last_used_at) : 0
		if (ta !== tb) return tb - ta
		return String(a?.name || '').localeCompare(String(b?.name || ''))
	})
})

const matchingAgents = computed(() => {
	const q = agentChipQuery.value.trim().toLowerCase()
	if (!q) return sortedAgents.value
	return sortedAgents.value.filter((a: any) => String(a?.name || '').toLowerCase().includes(q))
})

const visibleAgentChips = computed(() => {
	const all = matchingAgents.value
	if (showAllAgentChips.value || all.length <= AGENT_CHIP_LIMIT) return all
	// Selected agents win the slots, but the row keeps its recency order so
	// chips don't reshuffle under the cursor as the selection changes.
	const kept = new Set<string>()
	let budget = AGENT_CHIP_LIMIT
	for (const a of all) if (budget > 0 && isAgentSelected(a)) { kept.add(String(a.id)); budget-- }
	for (const a of all) if (budget > 0 && !kept.has(String(a.id))) { kept.add(String(a.id)); budget-- }
	return all.filter((a: any) => kept.has(String(a.id)))
})
const hiddenAgentChipCount = computed(() => matchingAgents.value.length - visibleAgentChips.value.length)

function isAgentSelected(agent: any) {
	if (props.agentsAreAuto) return false
	return (props.currentAgents || []).some((a: any) => String(a?.id) === String(agent?.id))
}

// Conversation starters from the selected agents, sourced from agent-scoped
// starter Prompts (not the legacy data_source.conversation_starters JSON).
// Each prompt's `text` is "Title\nDetailed prompt" — split into { title, prompt }.
const conversationStarters = ref<{ title: string; prompt: string }[]>([])
async function loadAgentStarters() {
	const ids = [...new Set((props.currentAgents || []).map((a: any) => a?.id).filter(Boolean))]
	if (!ids.length) { conversationStarters.value = []; return }
	const texts: string[] = []
	try {
		// Fetch starters for all selected agents in ONE batched request (union)
		// instead of one /prompts call per agent — a report with many attached
		// agents otherwise fired a request per agent just to fill 3 suggestions.
		const { data } = await useMyFetch(`/prompts?data_source_ids=${ids.join(',')}`)
		for (const p of ((data.value as any)?.prompts || [])) if (p?.text) texts.push(p.text)
	} catch { /* ignore */ }
	conversationStarters.value = [...new Set<string>(texts)].slice(0, 3).map((s: string) => {
		const nl = s.indexOf('\n')
		return nl === -1
			? { title: s, prompt: s }
			: { title: s.slice(0, nl).trim(), prompt: s.slice(nl + 1).trim() }
	})
}
// Key the watch on the actual set of agent ids (not a deep watch) so starters
// are refetched only when agents are added/removed, not on every nested change.
watch(
	() => [...new Set((props.currentAgents || []).map((a: any) => a?.id).filter(Boolean))].sort().join(','),
	loadAgentStarters,
	{ immediate: true },
)
</script>

<style scoped>
.fade-in {
    animation: fadeIn 0.6s ease-in;
}

@keyframes fadeIn {
    0% {
        opacity: 0;
        transform: translateY(10px);
    }
    100% {
        opacity: 1;
        transform: translateY(0);
    }
}
</style>
