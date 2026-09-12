<template>
	<!-- Draft report: the chrome of a report with nothing behind it. No row is
	     written until the first prompt — PromptBoxV2 creates the report and
	     navigates to it (see its createReport), the same path the home page
	     has always used. The composer sits in the same place as on a real
	     report so the hand-off doesn't move it. -->
	<div class="flex flex-col h-dvh overflow-y-hidden bg-white dark:bg-gray-900 relative">
		<header class="sticky top-0 bg-white dark:bg-gray-900 z-10 flex flex-col border-gray-200 dark:border-gray-700">
			<div class="flex flex-row pt-1 h-[40px] pb-1 pe-2 items-center">
				<GoBackChevron />
				<h1 class="text-sm md:text-start text-center w-[500px]">
					<span class="font-semibold text-sm p-1 text-gray-400 dark:text-gray-500">{{ $t('reports.newReport') }}</span>
				</h1>
			</div>
		</header>

		<!-- Where the conversation will go. Empty by definition. -->
		<div class="flex-1 overflow-y-auto flex items-center justify-center">
			<div class="px-4 text-center">
				<img :src="orgIconUrl || '/assets/logo-128.png'" alt="" class="h-10 max-w-[100px] object-contain mx-auto opacity-90" />
				<p class="mt-4 text-lg font-normal text-gray-500 dark:text-gray-400">{{ $t('home.whatCanIHelpWith') }}</p>
			</div>
		</div>

		<!-- Composer: same container as the report view, so creating the report
		     doesn't shift it. -->
		<div class="shrink-0 bg-white dark:bg-gray-900">
			<div :class="['mx-auto w-full', isExcel ? 'px-0' : 'px-0 max-w-none sm:px-4 sm:max-w-2xl']">
				<PromptBoxV2
					:project="draftProject"
					:projectSelectable="true"
					:initialSelectedDataSources="initialAgents"
					:initialMode="initialMode"
					:textareaContent="prefill"
					:compact="isExcel"
					@update:modelValue="(v: string) => prefill = v"
					@openInstructions="showInstructionsModal = true"
				/>
			</div>
		</div>

		<!-- Instructions panel, same affordance as the home page. -->
		<UModal v-model="showInstructionsModal" :ui="{ width: 'sm:max-w-3xl' }">
			<div class="h-[78vh] flex flex-col">
				<ReportAgentPanel
					:agents="instructionPanelAgents"
					:show-close="true"
					@close="showInstructionsModal = false"
					@starter-click="onInstructionStarter"
				/>
			</div>
		</UModal>
	</div>
</template>

<script setup lang="ts">
import PromptBoxV2 from '~/components/prompt/PromptBoxV2.vue'
import GoBackChevron from '@/components/excel/GoBackChevron.vue'
import ReportAgentPanel from '~/components/report/ReportAgentPanel.vue'
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
const { organization } = useOrganization()
const { data: currentUser } = useAuth()

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

const showInstructionsModal = ref(false)
const instructionPanelAgents = computed(() => [
	...(initialAgents.value.length ? initialAgents.value : (effectiveAgentObjects.value || [])),
	{ id: '__global__', name: 'Global', isGlobal: true },
])
const onInstructionStarter = (starter: string) => {
	const nl = (starter || '').indexOf('\n')
	prefill.value = nl === -1 ? starter : starter.slice(nl + 1).trim()
	showInstructionsModal.value = false
}

const orgIconUrl = computed(() => {
	const orgId = organization.value?.id
	const orgs = (currentUser.value as any)?.organizations || []
	const org = orgs.find((o: any) => o.id === orgId) || orgs[0]
	return org?.icon_url || null
})

onMounted(() => { if (projectId.value) fetchProjects() })
</script>
