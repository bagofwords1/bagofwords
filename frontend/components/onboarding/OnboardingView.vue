<template>
  <div class="bg-white border border-gray-200 rounded-2xl overflow-hidden shadow-sm">
    <div class="grid grid-cols-1 md:grid-cols-3">
      <!-- Left: Progress -->
      <aside class="p-8 md:p-10 border-b md:border-b-0 md:border-r border-gray-100 md:col-span-1">
        <div>
          <h1 class="text-lg font-semibold text-gray-900">Let's set you up for success!</h1>
          <p class="text-sm text-gray-500 mt-1">Complete these steps to get started</p>
        </div>

        <div class="mt-8">
          <div v-if="loading" class="text-gray-500 text-sm">Loading...</div>
          <div v-else class="space-y-5">
            <div
              v-for="(item, index) in stepsList"
              :key="item.key"
              class="flex items-start space-x-3"
              :class="{ 'opacity-70': item.status === 'pending' && !isCurrentStep(item.key) }"
            >
              <div
                class="flex-shrink-0 w-6 h-6 rounded-full flex items-center justify-center text-xs font-medium mt-0.5"
                :class="getStepIndicatorClass(item.status, isCurrentStep(item.key))"
              >
                <Icon v-if="item.status === 'done'" name="heroicons-check" class="w-3.5 h-3.5" />
                <span v-else>{{ index + 1 }}</span>
              </div>
              <div class="flex-1 min-w-0">
                <div class="text-sm font-medium" :class="isCurrentStep(item.key) ? 'text-gray-900' : 'text-gray-700'">
                  {{ item.title }}
                </div>
                <div class="text-xs text-gray-500 mt-0.5">{{ item.description }}</div>
              </div>
            </div>
          </div>
        </div>
      </aside>

      <!-- Right: Step details -->
      <main class="p-8 md:p-10 md:col-span-2">
        <div v-if="loading" class="flex items-center justify-center h-full text-gray-500">Loading...</div>

        <div v-else-if="props.forceCompleted || onboarding?.completed" class="flex items-center justify-center h-full">
          <div class="text-center max-w-md">
            <div class="w-16 h-16 bg-green-100 rounded-full flex items-center justify-center mx-auto mb-4">
              <Icon name="heroicons-check" class="w-8 h-8 text-green-600" />
            </div>
            <h2 class="text-xl font-semibold text-gray-900 mb-2">All set!</h2>
            <p class="text-gray-600 mb-6">You're ready to start using the app.</p>
            <div class="flex items-center justify-center gap-3">
              <button @click="router.push('/')" class="bg-gray-900 hover:bg-black text-white text-sm font-medium py-2.5 px-6 rounded-lg transition-colors">Go to Dashboard</button>
              <button @click="router.push('/onboarding/llm')" class="text-gray-700 bg-white border border-gray-200 hover:bg-gray-50 text-sm font-medium py-2.5 px-6 rounded-lg transition-colors">Back to setup</button>
            </div>
          </div>
        </div>

        <div v-else-if="onboarding?.dismissed" class="flex items-center justify-center h-full">
          <div class="text-center max-w-md">
            <div class="w-16 h-16 bg-gray-100 rounded-full flex items-center justify-center mx-auto mb-4">
              <Icon name="heroicons-clock" class="w-8 h-8 text-gray-600" />
            </div>
            <h2 class="text-xl font-semibold text-gray-900 mb-2">Setup paused</h2>
            <p class="text-gray-600 mb-6">You can complete the remaining steps anytime from settings.</p>
            <button @click="router.push('/')" class="bg-gray-900 hover:bg-black text-white text-sm font-medium py-2.5 px-6 rounded-lg transition-colors">Go to Dashboard</button>
          </div>
        </div>

        <div v-else class="max-w-xl">
          <div class="flex items-start space-x-4">
            <div class="w-10 h-10 bg-gray-50 rounded-lg flex items-center justify-center flex-shrink-0">
              <Icon :name="getCurrentStepIcon()" class="w-5 h-5 text-gray-700" />
            </div>
            <div class="flex-1">
              <h2 class="text-lg font-semibold text-gray-900 mb-2">{{ getCurrentStepTitle() }}</h2>
              <p class="text-gray-600 mb-6">{{ getCurrentStepDescription() }}</p>

              <div class="space-y-3">
                <div v-if="currentStepKey === 'llm_configured'" class="bg-gray-50 rounded-lg p-4 text-sm text-gray-700">
                  Set a default model and enable a provider to power the AI.
                </div>
                <div v-else-if="currentStepKey === 'data_source_created'" class="bg-gray-50 rounded-lg p-4 text-sm text-gray-700">
                  Connect your first data source (database, API, or files).
                </div>
                <div v-else-if="currentStepKey === 'schema_selected'" class="bg-gray-50 rounded-lg p-4 text-sm text-gray-700">
                  Choose the tables you want the system to use.
                </div>
                <div v-else-if="currentStepKey === 'instructions_added'" class="bg-gray-50 rounded-lg p-4 text-sm text-gray-700">
                  Add a few instructions to provide business context.
                </div>
              </div>

              <div class="mt-6 flex items-center gap-3">
                <button @click="goToCurrentStep" class="bg-gray-900 hover:bg-black text-white text-sm font-medium py-2.5 px-5 rounded-lg transition-colors">{{ getCurrentStepButtonText() }}</button>
                <button @click="goToNextStep" class="text-gray-700 bg-white border border-gray-200 hover:bg-gray-50 text-sm font-medium py-2.5 px-5 rounded-lg transition-colors">Next</button>
              </div>
            </div>
          </div>
        </div>
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
const props = defineProps<{ 
  forcedStepKey?: 'llm_configured'|'data_source_created'|'schema_selected'|'instructions_added',
  forceCompleted?: boolean
}>()

const router = useRouter()
const route = useRoute()
const { onboarding, fetchOnboarding, updateOnboarding } = useOnboarding()

const loading = ref(true)

onMounted(async () => {
  await fetchOnboarding()
  if (onboarding.value && !props.forceCompleted) {
    onboarding.value.completed = false as any
    onboarding.value.dismissed = false as any
  }
  if (!props.forceCompleted) {
    syncUrlWithStep()
  }
  loading.value = false
})

const stepsList = computed(() => {
  if (!onboarding.value) return []
  const m = new Map([
    ['llm_configured', { title: 'Configure LLM', description: 'Pick provider and default model' }],
    ['data_source_created', { title: 'Connect a data source', description: 'Set up a connection' }],
    ['schema_selected', { title: 'Select schema/tables', description: 'Choose tables to index' }],
    ['instructions_added', { title: 'Add instructions', description: 'Guide the AI with context' }],
  ])
  const order = ['llm_configured','data_source_created','schema_selected','instructions_added']
  return order.map((key) => ({
    key,
    title: m.get(key)?.title || (key as string),
    description: m.get(key)?.description || '',
    status: (onboarding.value!.steps as any)[key]?.status || 'pending'
  }))
})

const currentStepKey = computed(() => props.forcedStepKey || onboarding.value?.current_step)

if (!props.forceCompleted) {
  watch(currentStepKey, () => syncUrlWithStep())
}

function routeForStep(): string {
  switch (currentStepKey.value) {
    case 'llm_configured': return '/onboarding/llm'
    case 'data_source_created': return '/onboarding/data'
    case 'schema_selected': return '/onboarding/data/schema'
    case 'instructions_added': return '/onboarding/context'
    default: return '/onboarding/completed'
  }
}

function nextRouteForStep(): string {
  switch (currentStepKey.value) {
    case 'llm_configured': return '/onboarding/data'
    case 'data_source_created': return '/onboarding/data/schema'
    case 'schema_selected': return '/onboarding/context'
    case 'instructions_added': return '/onboarding/completed'
    default: return '/onboarding'
  }
}

function syncUrlWithStep() {
  if (props.forceCompleted) return
  if (!route.path.startsWith('/onboarding')) return
  const target = routeForStep()
  if (target !== route.path) router.replace(target)
}

function isCurrentStep(stepKey: string) {
  return currentStepKey.value === stepKey
}

function getStepIndicatorClass(status: string, isCurrent: boolean) {
  if (status === 'done') return 'bg-green-100 text-green-600'
  if (isCurrent) return 'bg-gray-900 text-white'
  return 'bg-gray-100 text-gray-500'
}

function getCurrentStepTitle() {
  const step = stepsList.value.find(s => s.key === currentStepKey.value)
  return step?.title || 'Get Started'
}

function getCurrentStepDescription() {
  const step = stepsList.value.find(s => s.key === currentStepKey.value)
  return step?.description || 'Complete the next step in your setup'
}

function getCurrentStepIcon() {
  switch (currentStepKey.value) {
    case 'llm_configured': return 'heroicons-cpu-chip'
    case 'data_source_created': return 'heroicons-circle-stack'
    case 'schema_selected': return 'heroicons-table-cells'
    case 'instructions_added': return 'heroicons-document-text'
    default: return 'heroicons-play'
  }
}

function getCurrentStepButtonText() {
  if (onboarding.value?.completed) return 'Completed'
  if (onboarding.value?.dismissed) return 'Resume Setup'
  switch (currentStepKey.value) {
    case 'llm_configured': return 'Configure Models'
    case 'data_source_created': return 'Add Data Source'
    case 'schema_selected': return 'Select Schema'
    case 'instructions_added': return 'Add Instructions'
    default: return 'Continue'
  }
}

function goToCurrentStep() {
  const ob = onboarding.value
  if (!ob || ob.dismissed || ob.completed) return router.push('/')
  switch (currentStepKey.value) {
    case 'llm_configured':
      return router.push('/settings/models')
    case 'data_source_created':
      return router.push('/integrations')
    case 'schema_selected':
      return router.push('/integrations')
    case 'instructions_added':
      return router.push('/instructions')
    default:
      return router.push('/')
  }
}

async function skipForNow() {
  await updateOnboarding({ dismissed: true })
  router.push('/')
}

function goToNextStep() {
  const next = nextRouteForStep()
  router.push(next)
}

</script>


