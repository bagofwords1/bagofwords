<template>
  <div class="tracked-changes-view font-sans">
    <template v-if="diffOps.length === 0">
      <span class="text-gray-400 dark:text-gray-600 italic text-xs">{{ $t('trackedChanges.noChanges', 'No changes') }}</span>
    </template>
    <template v-else>
      <template v-for="(op, i) in diffOps" :key="i">
        <span v-if="op.type === 0" class="tc-equal">{{ op.text }}</span>
        <span v-else-if="op.type === 1" class="tc-insert">{{ op.text }}</span>
        <span v-else-if="op.type === -1" class="tc-delete">{{ op.text }}</span>
      </template>
    </template>
  </div>
</template>

<script setup lang="ts">
import type { DiffOp } from '@/composables/useTrackedChanges'

defineProps<{
  diffOps: DiffOp[]
}>()
</script>

<style scoped>
.tracked-changes-view {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 13px;
  line-height: 1.55;
  color: #1f2937;
}
.tc-equal { color: inherit; }
.tc-insert {
  background: #dcfce7;
  color: #166534;
  text-decoration: none;
  border-radius: 2px;
}
.tc-delete {
  background: #fee2e2;
  color: #991b1b;
  text-decoration: line-through;
  border-radius: 2px;
}
</style>
