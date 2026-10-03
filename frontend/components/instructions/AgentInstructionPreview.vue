<template>
  <div data-testid="agent-instruction-preview">
    <div :id="contentId" :class="{ 'max-h-[120px] overflow-hidden': !expanded }" @focusin="expanded = true">
      <div ref="content" class="landing-instruction-content text-gray-500 dark:text-gray-400">
        <InstructionText :text="text" :references="references" :markdown="true" />
      </div>
    </div>
    <div v-if="overflows || $slots.actions" class="mt-3 flex items-center gap-4">
      <button v-if="overflows" type="button" :aria-expanded="expanded" :aria-controls="contentId" class="text-xs text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100" @click="expanded = !expanded">{{ $t(expanded ? 'agentLanding.readLess' : 'agentLanding.readMore') }}</button>
      <div v-if="$slots.actions" class="ms-auto flex items-center gap-4"><slot name="actions" /></div>
    </div>
  </div>
</template>

<script setup lang="ts">
import InstructionText from '~/components/instructions/InstructionText.vue'

const props = defineProps<{ text: string; references?: any[] }>()
const contentId = useId()
const content = ref<HTMLElement | null>(null)
const expanded = ref(false)
const overflows = ref(false)
let observer: ResizeObserver | undefined
const measure = () => { overflows.value = (content.value?.getBoundingClientRect().height || 0) > 120 }
onMounted(() => {
  observer = new ResizeObserver(measure)
  if (content.value) observer.observe(content.value)
  measure()
})
watch(() => props.text, async () => { expanded.value = false; await nextTick(); measure() })
onBeforeUnmount(() => observer?.disconnect())
</script>

<style scoped>
.landing-instruction-content { display: flow-root; overflow-wrap: anywhere; }
.landing-instruction-content :deep(.instruction-prose) { color: inherit; }
.landing-instruction-content :deep(p),
.landing-instruction-content :deep(li) { font-size: 13px; line-height: 1.7; }
</style>
