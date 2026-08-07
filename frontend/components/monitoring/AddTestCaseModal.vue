<template>
  <div>
    <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-6xl' }">
      <UCard>
        <!-- All of the authoring UI lives in TestCaseEditor so the right-hand
             pane in KnowledgeExplorer renders the same editor rather than a
             second copy of the expectations builder. This component is now only
             the dialog chrome around it. -->
        <TestCaseEditor
          ref="editorRef"
          :suite-id="suiteId"
          :case-id="caseId"
          :agent-id="agentId"
          @close="close"
          @created="(v: any) => emit('created', v)"
          @updated="(v: any) => emit('updated', v)"
        />

        <template #footer>
          <div class="flex items-center justify-end space-x-2">
            <UButton color="gray" variant="soft" @click="close">Cancel</UButton>
            <UButton
              :loading="editorRef?.isSaving"
              :disabled="editorRef?.initialLoading"
              color="blue"
              @click="() => editorRef?.save()"
            >Save</UButton>
            <UButton
              :loading="editorRef?.isRunning"
              :disabled="editorRef?.initialLoading"
              color="blue"
              variant="soft"
              @click="() => editorRef?.runNow()"
            >Save and Run</UButton>
          </div>
        </template>
      </UCard>
    </UModal>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import TestCaseEditor from '~/components/monitoring/TestCaseEditor.vue'

const props = defineProps<{ modelValue: boolean, suiteId: string, caseId?: string, agentId?: string }>()
const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'created', value: any): void
  (e: 'updated', value: any): void
}>()

const editorRef = ref<any | null>(null)
const isOpen = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})
const close = () => emit('update:modelValue', false)
</script>
