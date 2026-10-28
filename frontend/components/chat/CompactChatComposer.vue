<template>
    <!-- The compact composer: one auto-growing textarea and a round send button
         that turns into Stop while a run is streaming (when the owner can stop). -->
    <div class="flex items-end gap-1.5">
        <textarea ref="inputEl" v-model="draft" rows="1"
            class="flex-1 resize-none text-xs border border-gray-200 dark:border-gray-700 rounded-lg px-2.5 py-2 bg-white dark:bg-gray-900 text-gray-800 dark:text-gray-200 focus:outline-none focus:ring-1 focus:ring-blue-500 max-h-24 disabled:opacity-60"
            :placeholder="isStreaming ? streamingPlaceholder : placeholder"
            :disabled="disabled || (isStreaming && !canStop)"
            :aria-label="placeholder"
            data-testid="chat-input"
            @keydown.enter.exact.prevent="submit"
            @input="autoGrow" />
        <button v-if="isStreaming && canStop" type="button" @click="$emit('stop')"
            class="flex-shrink-0 w-8 h-8 rounded-full bg-gray-700 dark:bg-gray-600 text-white flex items-center justify-center hover:bg-gray-800"
            :aria-label="stopLabel" :title="stopLabel" data-testid="chat-stop">
            <Icon name="heroicons:stop-solid" class="w-3.5 h-3.5" />
        </button>
        <button v-else type="button" @click="submit" :disabled="disabled || isStreaming || !draft.trim()"
            class="flex-shrink-0 w-8 h-8 rounded-full text-white flex items-center justify-center hover:opacity-90 disabled:opacity-40 compact-send"
            :style="accentColor ? { backgroundColor: accentColor } : {}"
            :aria-label="sendLabel" :title="sendLabel" data-testid="chat-send">
            <Icon name="heroicons:paper-airplane" class="w-3.5 h-3.5 rtl-flip" />
        </button>
    </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'

const props = withDefaults(defineProps<{
    isStreaming?: boolean
    disabled?: boolean
    canStop?: boolean
    placeholder?: string
    streamingPlaceholder?: string
    sendLabel?: string
    stopLabel?: string
    accentColor?: string
}>(), {
    isStreaming: false,
    disabled: false,
    canStop: false,
    placeholder: 'Ask a question...',
    streamingPlaceholder: 'Answering...',
    sendLabel: 'Send',
    stopLabel: 'Stop',
    accentColor: '',
})

const emit = defineEmits<{ (e: 'send', content: string): void; (e: 'stop'): void }>()

const draft = ref('')
const inputEl = ref<HTMLTextAreaElement | null>(null)

function submit() {
    const content = draft.value.trim()
    if (!content || props.isStreaming || props.disabled) return
    draft.value = ''
    if (inputEl.value) inputEl.value.style.height = ''
    emit('send', content)
}

function autoGrow() {
    const el = inputEl.value
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, 96)}px`
}

function focus() { inputEl.value?.focus() }
function setDraft(value: string) { draft.value = value; autoGrow(); focus() }

defineExpose({ focus, setDraft })
</script>

<style scoped>
.compact-send { background-color: rgb(37 99 235); }
</style>
