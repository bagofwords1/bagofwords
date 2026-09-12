<template>
	<!-- Draft report: the chrome of a report with nothing behind it. No row is
	     written until the first prompt — PromptBoxV2 creates the report and
	     navigates to it (see its createReport), the same path the home page
	     has always used. The body is the report view's own empty state and the
	     composer sits in the same place, so the hand-off changes nothing on
	     screen except the conversation appearing. -->
	<div class="flex flex-col h-dvh overflow-y-hidden bg-white dark:bg-gray-900 relative">
		<header class="sticky top-0 bg-white dark:bg-gray-900 z-10 flex flex-col border-gray-200 dark:border-gray-700">
			<div class="flex flex-row pt-1 h-[40px] pb-1 pe-2 items-center">
				<GoBackChevron />
				<h1 class="text-sm md:text-start text-center w-[500px]">
					<span class="font-semibold text-sm p-1 text-gray-400 dark:text-gray-500">{{ $t('reports.newReport') }}</span>
				</h1>
			</div>
		</header>

		<!-- Where the conversation will go. Same scroll container and column as
		     the report view, so the empty state lands in the same spot. -->
		<div class="flex-1 overflow-y-auto mt-4 pb-4">
			<div class="ps-3 pe-3 sm:ps-4 sm:pe-2 pb-[3px] max-w-2xl w-full mx-auto">
				<ReportEmptyState
					:mode="currentMode"
					:available-agents="availableAgents"
					:current-agents="currentAgents"
					:agents-are-auto="agentsAreAuto"
					@toggle-agent="toggleAgentSelection"
					@starter="handleExampleClick"
				/>
			</div>
		</div>

		<!-- Composer: same container as the report view. -->
		<div class="shrink-0 bg-white dark:bg-gray-900">
			<div :class="['mx-auto w-full', isExcel ? 'px-0' : 'px-0 max-w-none sm:px-4 sm:max-w-2xl']">
				<PromptBoxV2
					ref="promptBoxRef"
					:project="draftProject"
					:projectSelectable="true"
					:initialSelectedDataSources="initialAgents"
					:initialMode="initialMode"
					:textareaContent="prefill"
					:compact="isExcel"
					@update:modelValue="(v: string) => prefill = v"
					@update:selectedDataSources="(val: any[]) => currentAgents = val"
					@update:availableDataSources="(val: any[]) => availableAgents = val"
					@update:autoMode="(val: boolean) => agentsAreAuto = val"
					@update:mode="(m: any) => currentMode = m"
					@openInstructions="showInstructionsModal = true"
				/>
			</div>
		</div>

		<!-- Instructions panel, same affordance as the report view's agent tab. -->
		<UModal v-model="showInstructionsModal" :ui="{ width: 'sm:max-w-3xl' }">
			<div class="h-[78vh] flex flex-col">
				<ReportAgentPanel
					:agents="instructionPanelAgents"
					:show-close="true"
					@close="showInstructionsModal = false"
					@starter-click="handleExampleClick"
				/>
			</div>
		</UModal>
	</div>
</template>

<script setup lang="ts">
import PromptBoxV2 from '~/components/prompt/PromptBoxV2.vue'
import GoBackChevron from '@/components/excel/GoBackChevron.vue'
import ReportAgentPanel from '~/components/report/ReportAgentPanel.vue'
import ReportEmptyState from '~/components/report/ReportEmptyState.vue'
import { useExcel } from '~/composables/useExcel'

definePageMeta({
	layout: 'default',
	auth: true,
	permissions: ['create_reports']
})

const route = useRoute()
const { isExcel } = useExcel()
const { agents, selectedAgentObjects, effectiveAgentObjects } = useAgent()
const { projects, fetchProjects } = useProjects()

// ── Draft context, all carried in the query string ──────────────────────
// A draft has no row to hang context on, so the entry point that opened it
// passes what it knows: ?project= (the folder it was started in), ?agents=
// (a comma-separated scope, e.g. "New report" on an agent), ?mode=training,
// and ?prompt= (prefilled, never auto-submitted).
const projectId = computed(() => {
	const q = route.query.project
	return typeof q === 'string' && q ? q : null
})
const agentIds = computed(() => {
	const q = route.query.agents
	return (typeof q === 'string' ? q : '').split(',').map(s => s.trim()).filter(Boolean)
})
const initialMode = computed(() => (route.query.mode === 'training' ? 'training' : 'chat') as 'chat' | 'training')
const prefill = ref(typeof route.query.prompt === 'string' ? route.query.prompt : '')

// The project chip. Resolved from the loaded list for display only — the id
// that actually lands on the report comes from the route, so submitting
// before this resolves still files the report correctly.
const draftProject = computed(() => {
	if (!projectId.value) return null
	const p = (projects.value || []).find((x: any) => String(x.id) === String(projectId.value))
	return p ? { id: p.id, name: p.name, color: p.color } : null
})

// Which agents the composer opens with:
// - ?agents= — an entry point that named a scope (the agent page). Explicit,
//   so it overrides project defaults server-side.
// - inside a project — nothing, which is Auto, which the backend fills with
//   the project's default agents. Same payload the project page used to POST.
// - otherwise — the agents the user pinned workspace-wide, empty under Auto.
// Picks made here ride on the created report only; unlike the home page this
// never writes back to the user's saved selection, since a draft opened for
// one agent shouldn't repin their whole workspace.
const initialAgents = computed(() => {
	if (agentIds.value.length) {
		const byId = new Map((agents.value || []).map((a: any) => [String(a.id), a]))
		return agentIds.value.map(id => byId.get(String(id))).filter(Boolean)
	}
	if (projectId.value) return []
	return selectedAgentObjects.value
})

// Live state published by the prompt box, exactly as the report view consumes
// it — the empty state renders the selection, the box owns it.
const promptBoxRef = ref<any>(null)
const currentAgents = ref<any[]>([])
const availableAgents = ref<any[]>([])
const agentsAreAuto = ref(false)
const currentMode = ref<'chat' | 'training'>(initialMode.value)

// Route the click back through the prompt box's selector so the empty-state
// picker and the dropdown stay one selection.
function toggleAgentSelection(agent: any) {
	promptBoxRef.value?.toggleDataSource?.(agent)
}

// A starter is a prompt, so it sends — which on a draft is what creates the
// report. Same behaviour as clicking a starter on an empty report.
function handleExampleClick(starter: string) {
	if (!starter) return
	showInstructionsModal.value = false
	promptBoxRef.value?.submitPrompt?.(starter)
}

const showInstructionsModal = ref(false)
const instructionPanelAgents = computed(() => [
	...(currentAgents.value.length ? currentAgents.value : (effectiveAgentObjects.value || [])),
	{ id: '__global__', name: 'Global', isGlobal: true },
])

onMounted(() => { if (projectId.value) fetchProjects() })
</script>
