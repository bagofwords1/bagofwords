<template>
  <div class="rounded border border-gray-150 bg-gray-50 mx-1 mb-1">
    <!-- Row header -->
    <div
      class="flex items-start gap-2 px-3 py-1.5 hover:bg-gray-100 rounded"
      :class="!selected ? 'opacity-60' : ''"
    >
      <UCheckbox
        :model-value="selected"
        color="blue"
        @update:model-value="$emit('update:selected', $event)"
        @click.stop
        class="mt-0.5"
      />
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
      <div class="flex items-center gap-1 shrink-0 mt-0.5">
        <template v-if="resolution === 'accepted'">
          <span class="inline-flex items-center gap-0.5 text-[10px] text-green-600 px-1">
            <Icon name="heroicons:check-circle" class="w-3 h-3" />
            {{ $t('prompt.accepted', 'Accepted') }}
          </span>
        </template>
        <template v-else-if="resolution === 'rejected'">
          <span class="inline-flex items-center gap-0.5 text-[10px] text-gray-400 px-1">
            <Icon name="heroicons:x-circle" class="w-3 h-3" />
            {{ $t('prompt.rejected', 'Rejected') }}
          </span>
        </template>
        <template v-else>
          <button
            class="inline-flex items-center gap-0.5 px-1.5 py-0.5 text-[10px] font-medium text-green-700 bg-green-50 border border-green-200 rounded hover:bg-green-100 transition-colors disabled:opacity-50"
            :disabled="isAccepting || isRejecting"
            @click.stop="handleAccept"
          >
            <Spinner v-if="isAccepting" class="w-2.5 h-2.5 text-green-600" />
            <Icon v-else name="heroicons:check" class="w-2.5 h-2.5" />
            {{ $t('prompt.acceptShort', 'Accept') }}
          </button>
          <button
            class="inline-flex items-center gap-0.5 px-1.5 py-0.5 text-[10px] font-medium text-gray-600 bg-white border border-gray-200 rounded hover:bg-gray-50 transition-colors disabled:opacity-50"
            :disabled="isAccepting || isRejecting"
            @click.stop="handleReject"
          >
            <Spinner v-if="isRejecting" class="w-2.5 h-2.5 text-gray-400" />
            <Icon v-else name="heroicons:x-mark" class="w-2.5 h-2.5" />
            {{ $t('prompt.rejectShort', 'Reject') }}
          </button>
          <button
            class="text-[10px] text-gray-500 hover:text-gray-800 px-1.5 py-0.5 rounded hover:bg-gray-200"
            @click.stop="$emit('open')"
          >
            {{ $t('prompt.openInstruction', 'Open') }}
          </button>
        </template>
      </div>
    </div>

    <!-- Expanded content -->
    <div v-if="isExpanded" class="px-3 pb-2">
      <div v-if="isLoading" class="flex items-center gap-2 py-3">
        <Spinner class="w-3 h-3" />
        <span class="text-[11px] text-gray-500">{{ $t('tools.editInstruction.loadingDiff', 'Loading…') }}</span>
      </div>

      <!-- Diff view for edits with a previous version -->
      <div v-else-if="inst.isEdit && previousText !== null && previousText !== ''" class="border border-gray-200 rounded overflow-hidden bg-white">
        <ClientOnly>
          <MonacoDiffEditor
            :original="previousText"
            :modified="currentText"
            height="160px"
            language="plaintext"
          />
        </ClientOnly>
      </div>

      <!-- Markdown view for creates or edits with no prior version -->
      <div
        v-else-if="currentText"
        dir="auto"
        class="text-[12px] text-gray-800 leading-relaxed bg-white border border-gray-200 rounded p-2 instruction-content"
      >
        <MDC :value="currentText" class="markdown-content" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import Spinner from '@/components/Spinner.vue'
import MonacoDiffEditor from '@/components/MonacoDiffEditor.vue'

interface PendingInstruction {
  instructionId: string
  title: string
  category: string
  isEdit: boolean
  lineCount: number
}

interface Props {
  inst: PendingInstruction
  selected: boolean
}

const props = defineProps<Props>()
const emit = defineEmits<{
  (e: 'update:selected', v: boolean): void
  (e: 'open'): void
  (e: 'accept'): void
  (e: 'reject'): void
}>()

const isExpanded = ref(false)
const isLoading = ref(false)
const currentText = ref<string>('')
const previousText = ref<string | null>(null)
const hasFetched = ref(false)
const isAccepting = ref(false)
const isRejecting = ref(false)
const resolution = ref<'accepted' | 'rejected' | null>(null)

function handleAccept() {
  if (isAccepting.value) return
  isAccepting.value = true
  resolution.value = 'accepted'
  emit('accept')
  // Parent owns the actual API call; UI optimistically reflects resolved.
  setTimeout(() => { isAccepting.value = false }, 600)
}

function handleReject() {
  if (isRejecting.value) return
  isRejecting.value = true
  resolution.value = 'rejected'
  emit('reject')
  setTimeout(() => { isRejecting.value = false }, 600)
}

function toggleExpanded() {
  isExpanded.value = !isExpanded.value
}

watch(isExpanded, async (expanded) => {
  if (expanded && !hasFetched.value) {
    await fetchData()
  }
})

async function fetchData() {
  if (!props.inst.instructionId) return
  isLoading.value = true
  try {
    // Pull text from the versions list rather than the instruction row —
    // a staged edit lives as a new version while instruction.text is still
    // the previously-published value until the build is approved.
    const { data: versionsData } = await useMyFetch(
      `/instructions/${props.inst.instructionId}/versions?limit=50`
    )
    const items = ((versionsData.value as any)?.items) || []
    const sorted = [...items].sort((a: any, b: any) => (b.version_number || 0) - (a.version_number || 0))

    if (props.inst.isEdit && sorted.length >= 2) {
      const [curRes, prevRes] = await Promise.all([
        useMyFetch(`/instructions/${props.inst.instructionId}/versions/${sorted[0].id}`),
        useMyFetch(`/instructions/${props.inst.instructionId}/versions/${sorted[1].id}`),
      ])
      currentText.value = ((curRes.data.value as any)?.text) || ''
      previousText.value = ((prevRes.data.value as any)?.text) || ''
    } else if (sorted.length >= 1) {
      const { data: curData } = await useMyFetch(
        `/instructions/${props.inst.instructionId}/versions/${sorted[0].id}`
      )
      currentText.value = ((curData.value as any)?.text) || ''
    } else {
      const { data: instData } = await useMyFetch(`/instructions/${props.inst.instructionId}`)
      currentText.value = ((instData.value as any)?.text) || ''
    }
    hasFetched.value = true
  } catch (e) {
    console.error('Failed to fetch instruction details:', e)
  } finally {
    isLoading.value = false
  }
}
</script>

<style scoped>
.instruction-content :deep(.markdown-content) {
  font-size: 12px;
  line-height: 1.5;
}
.instruction-content :deep(.markdown-content p) {
  margin: 0 0 0.5em 0;
}
.instruction-content :deep(.markdown-content p:last-child) {
  margin-bottom: 0;
}
.instruction-content :deep(.markdown-content code) {
  font-size: 10px;
  padding: 0.1em 0.3em;
  background: rgba(0, 0, 0, 0.05);
  border-radius: 3px;
}
.instruction-content :deep(.markdown-content pre) {
  font-size: 10px;
  padding: 0.5em;
  background: rgba(0, 0, 0, 0.03);
  border-radius: 4px;
  overflow-x: auto;
  margin: 0.5em 0;
}
.instruction-content :deep(.markdown-content ul),
.instruction-content :deep(.markdown-content ol) {
  margin: 0.5em 0;
  padding-left: 1.5em;
}
.instruction-content :deep(.markdown-content li) {
  margin: 0.2em 0;
}
</style>
