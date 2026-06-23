<template>
  <div class="rounded border border-gray-150 bg-gray-50 mx-1 mb-1">
    <!-- Row header -->
    <div class="flex items-start gap-2 px-3 py-1.5">
      <div class="flex-1 min-w-0 cursor-pointer" @click="toggleExpanded">
        <div class="flex items-center gap-1.5">
          <Icon
            :name="isExpanded ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
            class="w-3 h-3 text-gray-400 shrink-0 rtl-flip"
          />
          <span
            :class="[
              'text-[9px] font-mono font-semibold uppercase tracking-wide',
              inst.isEdit ? 'text-blue-600' : 'text-green-600'
            ]"
          >
            {{ inst.isEdit ? $t('prompt.changeEdit', 'edit') : $t('prompt.changeNew', 'new') }}
          </span>
          <span dir="auto" class="text-[12px] text-gray-700 truncate hover:text-gray-900">{{ inst.title }}</span>
          <span v-if="inst.lineCount > 0" class="text-[10px] font-mono text-green-600 shrink-0">+{{ inst.lineCount }}</span>
        </div>
        <div v-if="inst.category" class="text-[10px] text-gray-400 mt-0.5 ms-[18px]">{{ inst.category }}</div>
      </div>
      <button
        class="text-[10px] text-gray-500 hover:text-gray-800 px-1.5 py-0.5 rounded hover:bg-gray-200 shrink-0 mt-0.5"
        @click.stop="$emit('open')"
      >
        {{ $t('prompt.openInstruction', 'Open') }}
      </button>
    </div>

    <!-- Inline per-hunk tracked changes (Google-docs style). Accept / reject
         each hunk; resolution is applied to main server-side immediately. -->
    <div v-if="isExpanded" class="px-1 pb-2">
      <div class="border border-gray-200 rounded overflow-hidden bg-white">
        <InstructionTrackedChanges
          :instruction-id="inst.instructionId"
          :can-approve="canApprove"
          compact
          collapse-context
          @changed="$emit('changed')"
          @empty="$emit('changed')"
        />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import InstructionTrackedChanges from '@/components/instructions/InstructionTrackedChanges.vue'

interface PendingInstruction {
  instructionId: string
  title: string
  category: string
  isEdit: boolean
  lineCount: number
}

defineProps<{
  inst: PendingInstruction
  canApprove?: boolean
}>()

defineEmits<{
  (e: 'open'): void
  (e: 'changed'): void
}>()

// Expanded by default so the inline accept/reject controls are reachable
// without an extra click — matches the editor's tracked-changes experience.
const isExpanded = ref(true)

function toggleExpanded() {
  isExpanded.value = !isExpanded.value
}
</script>
