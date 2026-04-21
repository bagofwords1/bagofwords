<template>
  <div class="bg-white border border-gray-200 rounded-2xl overflow-hidden shadow-sm" :class="props.hideSidebar ? 'md:w-2/3 mx-auto' : ''">
    <div class="grid grid-cols-1 md:grid-cols-3">
      <!-- Left: Progress -->
      <aside v-if="!props.hideSidebar" class="p-8 md:p-10 border-b md:border-b-0 md:border-e border-gray-100 md:col-span-1">
        <div>
            <img src="/assets/logo-128.png" alt="Logo" class="w-10 h-10 mb-5" />
          <h1 class="text-lg font-semibold text-gray-900">{{ $t('onboarding.welcome') }}</h1>
          <p class="text-sm text-gray-500 mt-1">{{ $t('onboarding.welcomeSubtitle') }}</p>
        </div>

        <div class="mt-8">
          <div v-if="loading" class="text-gray-500 text-sm">{{ $t('onboarding.loading') }}</div>
          <div v-else class="space-y-5">
            <div
              v-if="currentStepKey !== 'onboarding'"
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
      <main class="p-8 md:p-10" :class="props.hideSidebar ? 'md:col-span-3' : 'md:col-span-2'">
        <div :class="props.hideSidebar ? '' : ''">
        <Transition name="fade" mode="out-in">
          <div v-if="loading" key="loading" class="flex items-center justify-center h-full text-gray-500">{{ $t('onboarding.loading') }}</div>

          

          <!-- Removed 'Setup paused' state; users can always continue onboarding -->

          <div v-else :key="'step-' + currentStepKey" class="max-w-xl">
            <div class="flex items-start space-x-4">
              <div class="flex-1">
                <img src="/assets/logo-128.png" alt="Logo" class="w-10 h-10 mb-5" v-if="props.hideSidebar" />
                <h2 class="text-lg font-semibold text-gray-900 mb-2">{{ getCurrentStepTitle() }}</h2>
                <p class="text-gray-600 mb-6">{{ getCurrentStepDescription() }}</p>

                <div class="space-y-3">
                  <slot name="onboarding" v-if="currentStepKey === 'onboarding'"></slot>
                  <slot name="llm" v-if="currentStepKey === 'llm_configured'"></slot>
                  <slot name="data" v-else-if="currentStepKey === 'data_source_created'"></slot>
                  <slot name="schema" v-else-if="currentStepKey === 'schema_selected'"></slot>
                  <slot name="instructions" v-else-if="currentStepKey === 'instructions_added'"></slot>
                </div>

                <div class="mt-6 flex items-center gap-3">
                  <button @click="goToCurrentStep" class="bg-gray-900 hidden hover:bg-black text-white text-sm font-medium py-2.5 px-5 rounded-lg transition-colors">{{ getCurrentStepButtonText() }}</button>
                  <button v-if="!props.hideNextButton" @click="goToNextStep" class="text-gray-700 bg-white border border-gray-200 hover:bg-gray-50 text-sm font-medium py-2.5 px-5 rounded-lg transition-colors">{{ $t('onboarding.next') }}</button>
                </div>
              </div>
            </div>
          </div>
        </Transition>
        </div>
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
const props = defineProps<{ 
  forcedStepKey?: 'onboarding'|'llm_configured'|'data_source_created'|'schema_selected'|'instructions_added',
  forceCompleted?: boolean,
  hideNextButton?: boolean,
  hideSidebar?: boolean
}>()

const router = useRouter()
const route = useRoute()
const { onboarding, fetchOnboarding, updateOnboarding } = useOnboarding()
const { t } = useI18n()

const loading = ref(true)

onMounted(async () => {
  await fetchOnboarding({ in_onboarding: true })
  if (!props.forceCompleted) {
    syncUrlWithStep()
  }
  loading.value = false
})

// Complete step metadata, used for titles/descriptions irrespective of sidebar order
const stepMeta = computed(() => new Map([
  ['onboarding', { title: t('onboarding.steps.onboardingTitle'), description: t('onboarding.steps.onboardingDescription') }],
  ['llm_configured', { title: t('onboarding.steps.llmTitle'), description: t('onboarding.steps.llmDescription') }],
  ['data_source_created', { title: t('onboarding.steps.dataTitle'), description: t('onboarding.steps.dataDescription') }],
  ['schema_selected', { title: t('onboarding.steps.schemaTitle'), description: t('onboarding.steps.schemaDescription') }],
  ['instructions_added', { title: t('onboarding.steps.instructionsTitle'), description: t('onboarding.steps.instructionsDescription') }],
]))

const stepsList = computed(() => {
  if (!onboarding.value) return []
  // Exclude schema_selected from the sidebar, it is merged into Connect data
  const order = ['llm_configured','data_source_created','instructions_added']
  return order.map((key) => {
    let statusValue = (onboarding.value!.steps as any)[key]?.status || 'pending'

    // While on schema step, treat Connect data as the current/in-progress step
    if (key === 'data_source_created' && currentStepKey.value === 'schema_selected') {
      statusValue = 'in_progress'
    }

    return {
      key,
      title: stepMeta.value.get(key)?.title || (key as string),
      description: stepMeta.value.get(key)?.description || '',
      status: statusValue
    }
  })
})

const currentStepKey = computed(() => props.forcedStepKey || onboarding.value?.current_step)

if (!props.forceCompleted) {
  watch(currentStepKey, () => syncUrlWithStep())
}

// Redirect immediately when onboarding is completed
watch(() => onboarding.value?.completed, (isCompleted) => {
  if (isCompleted && route.path.startsWith('/onboarding')) {
    router.replace('/?setup=done')
  }
})

function routeForStep(): string {
  switch (currentStepKey.value) {
    case 'onboarding': return '/onboarding'
    case 'llm_configured': return '/onboarding/llm'
    case 'data_source_created': return '/onboarding/data'
    case 'schema_selected': return '/onboarding/data/schema'
    case 'instructions_added':
      return route.params?.ds_id ? `/onboarding/data/${String(route.params.ds_id)}/context` : '/onboarding/context'
    default: return '/onboarding'
  }
}

function nextRouteForStep(): string {
  switch (currentStepKey.value) {
    case 'onboarding': return '/onboarding/llm'
    case 'llm_configured': return '/onboarding/data'
    case 'data_source_created': return '/onboarding/data/schema'
    case 'schema_selected':
      return route.params?.ds_id ? `/onboarding/data/${String(route.params.ds_id)}/context` : '/onboarding/context'
    case 'instructions_added': return '/'
    default: return '/onboarding'
  }
}

function syncUrlWithStep() {
  if (props.forceCompleted) return
  // If onboarding is completed, immediately redirect out of onboarding
  if (onboarding.value?.completed) {
    if (route.path.startsWith('/onboarding')) router.replace('/?setup=done')
    return
  }
  // Allow onboarding landing page to render without auto-redirect
  if (route.path === '/onboarding') return
  // If the user is on schema/context sub-steps, never auto-redirect away.
  const isSchemaRoute = /^\/onboarding\/data(\/[\w-]+)?\/schema(\/?|$)/.test(route.path)
  const isContextRoute = /^\/onboarding\/data(\/[\w-]+)?\/context(\/?|$)/.test(route.path) || route.path.startsWith('/onboarding/context')
  const stepKey = currentStepKey.value
  if (isSchemaRoute || isContextRoute || stepKey === 'schema_selected' || stepKey === 'instructions_added') {
    // Let the schema/context flow proceed.
    return
  }
  // Decide routing based on LLM/Data step status for top-level onboarding pages
  const steps: any = (onboarding.value && (onboarding.value as any).steps) || {}
  const llmDone = steps.llm_configured?.status === 'done'
  const dataDone = steps.data_source_created?.status === 'done'
  const schemaDone = steps.schema_selected?.status === 'done'
  const instructionsDone = steps.instructions_added?.status === 'done'

  // If models and data are done but schema/context are pending → stay in onboarding
  if (llmDone && dataDone) {
    // Prefer schema selection if not completed
    if (!schemaDone) {
      const target = '/onboarding/data/schema'
      if (route.path !== target && !/^\/onboarding\/data(\/[\w-]+)?\/schema(\/?|$)/.test(route.path)) router.replace(target)
      return
    }
    // If schema is done but instructions/context pending → go to context
    if (!instructionsDone) {
      const target = '/onboarding/context'
      const isContextRoute = route.path.startsWith('/onboarding/context') ||
        /^\/onboarding\/data(\/[\w-]+)?\/context(\/?|$)/.test(route.path)
      if (!isContextRoute) router.replace(target)
      return
    }
    // Everything is done → home
    if (route.path.startsWith('/onboarding')) router.replace('/?setup=done')
    return
  }
  // If only LLM done → go to Connect data
  if (llmDone && !dataDone) {
    const target = '/onboarding/data'
    if (route.path !== target) router.replace(target)
    return
  }
  // If only data done (and LLM not) → home
  if (!llmDone && dataDone) {
    if (route.path.startsWith('/onboarding')) router.replace('/')
    return
  }
  if (!route.path.startsWith('/onboarding')) return
  const targetBase = routeForStep()
  // Allow schema step to be either /onboarding/data/schema or /onboarding/data/:id/schema
  if (currentStepKey.value === 'schema_selected') {
    const schemaOk = /^\/onboarding\/data(\/[\w-]+)?\/schema(\/?|$)/.test(route.path)
    if (!schemaOk) router.replace(targetBase)
    return
  }
  // Allow instructions step to be either /onboarding/context or /onboarding/data/:id/context
  if (currentStepKey.value === 'instructions_added') {
    const instructionsOk = route.path.startsWith('/onboarding/context') ||
      /^\/onboarding\/data(\/[\w-]+)?\/context(\/?|$)/.test(route.path)
    if (!instructionsOk) router.replace(targetBase)
    return
  }
  if (!route.path.startsWith(targetBase)) router.replace(targetBase)
}

function isCurrentStep(stepKey: string) {
  // Treat schema as part of Connect data for highlighting
  if (stepKey === 'data_source_created' && currentStepKey.value === 'schema_selected') return true
  return currentStepKey.value === stepKey
}

function getStepIndicatorClass(status: string, isCurrent: boolean) {
  if (status === 'done') return 'bg-green-100 text-green-600'
  if (isCurrent) return 'bg-gray-900 text-white'
  return 'bg-gray-100 text-gray-500'
}

function getCurrentStepTitle() {
  const key = (currentStepKey.value || '') as string
  return stepMeta.value.get(key)?.title || t('onboarding.fallbackTitle')
}

function getCurrentStepDescription() {
  const key = (currentStepKey.value || '') as string
  return stepMeta.value.get(key)?.description || t('onboarding.fallbackDescription')
}

function getCurrentStepIcon() {
  switch (currentStepKey.value) {
    case 'onboarding': return 'heroicons-play'
    case 'llm_configured': return 'heroicons-cpu-chip'
    case 'data_source_created': return 'heroicons-circle-stack'
    case 'schema_selected': return 'heroicons-table-cells'
    case 'instructions_added': return 'heroicons-document-text'
    default: return 'heroicons-play'
  }
}

function getCurrentStepButtonText() {
  if (onboarding.value?.completed) return t('onboarding.completed')
  if (onboarding.value?.dismissed) return t('onboarding.resumeSetup')
  switch (currentStepKey.value) {
    case 'onboarding': return t('onboarding.steps.onboardingButton')
    case 'llm_configured': return t('onboarding.steps.llmButton')
    case 'data_source_created': return t('onboarding.steps.dataButton')
    case 'schema_selected': return t('onboarding.steps.schemaButton')
    case 'instructions_added': return t('onboarding.steps.instructionsButton')
    default: return t('onboarding.steps.continue')
  }
}

function goToCurrentStep() {
  const ob = onboarding.value
  if (!ob || ob.dismissed || ob.completed) return router.push('/')
  switch (currentStepKey.value) {
    case 'onboarding':
      return router.push('/onboarding')
    case 'llm_configured':
      return router.push('/settings/models')
    case 'data_source_created':
      return router.push('/data')
    case 'schema_selected':
      return router.push('/data')
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


