<template>
  <div class="tracked-changes-view font-sans">
    <template v-if="diffOps.length === 0">
      <span class="text-ink-muted italic text-xs">{{ $t('trackedChanges.noChanges', 'No changes') }}</span>
    </template>
    <!-- Rendered as markdown, like the instruction itself: a raw-text diff
         showed `**`, `#` and table pipes literally, and its `dir="auto"` put
         every Hebrew line that opens with an English word left-to-right. -->
    <InstructionDiffProse v-else :pieces="pieces" variant="highlight" />
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { DiffOp } from '@/composables/useTrackedChanges'
import InstructionDiffProse from '~/components/instructions/InstructionDiffProse.vue'
import { piecesFromDiffOps } from '~/utils/markdownDiff'

const props = defineProps<{
  diffOps: DiffOp[]
}>()

const pieces = computed(() => piecesFromDiffOps(props.diffOps))
</script>

<style scoped>
.tracked-changes-view {
  word-break: break-word;
}
</style>
