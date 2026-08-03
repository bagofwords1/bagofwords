<template>
  <div class="min-h-screen py-10 px-4 md:w-2/3 lg:w-1/2 mx-auto text-sm">
    <div class="w-full px-4 ps-0 py-4">
      <div>
        <h1 class="text-lg font-semibold text-center">Configure knowledge</h1>
        <p class="mt-4 text-gray-500 dark:text-gray-400 text-center">
          {{ subtitle }}
        </p>
      </div>
      <WizardSteps class="mb-5 mt-4" current="schema" :ds-id="id" />
      <AgentKnowledgeTabs :ds-id="id" continue-label="Save & Continue" @saved="onSaved" @kinds="kinds = $event" />
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true })
import WizardSteps from '@/components/datasources/WizardSteps.vue'
import AgentKnowledgeTabs from '@/components/datasources/AgentKnowledgeTabs.vue'
import { knowledgeStepHint, type KnowledgeKind } from '~/composables/useCatalogCount'
const route = useRoute()
const router = useRouter()
const id = computed(() => String(route.params.id || ''))
// Filled in once the tabs have loaded the agent's connections, so the blurb
// only mentions the tabs that are actually there.
const kinds = ref<KnowledgeKind[]>([])
const subtitle = computed(() => knowledgeStepHint(kinds.value))
function onSaved() { router.replace(`/agents/new/${id.value}/context`) }
</script>
