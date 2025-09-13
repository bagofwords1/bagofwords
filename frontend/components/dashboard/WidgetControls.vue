<template>
  <div v-if="edit" class="absolute right-1 top-1 z-20 flex gap-1 p-1 rounded ">
    <button v-if="!isText" title="Remove Widget" class="text-xs items-center flex gap-0.5 hover:bg-red-100 text-red-400 px-1 py-0.5 rounded " @click="$emit('remove')">
      <Icon name="heroicons:trash" class="w-3 h-3"/>
    </button>
    <button v-if="isText && !isNew" title="Remove Text" class="text-xs items-center flex gap-0.5 hover:bg-red-100 text-red-400 px-1 py-0.5 rounded " @click="$emit('removeText')">
      <Icon name="heroicons:trash" class="w-3 h-3"/>
    </button>
    <button v-if="isVisualization && queryId" title="Edit Query" class="text-xs items-center flex gap-0.5 hover:bg-blue-100 text-blue-400 px-1 py-0.5 rounded " @click="handleEditVisualization">
      <Icon name="heroicons:pencil-square" class="w-3 h-3"/>
    </button>
    <button v-if="isText" :title="isNew ? 'Cancel Adding Text' : 'Edit Text'" class="text-xs items-center flex gap-0.5 hover:bg-blue-100 text-blue-400 px-1 py-0.5 rounded " @click="$emit('toggleTextEdit')">
      <Icon name="heroicons:pencil" v-if="!isEditing && !isNew" class="w-3 h-3"/>
      <Icon name="heroicons:x-mark" v-else class="w-3 h-3"/>
    </button>
  </div>
</template>

<script setup lang="ts">
const props = defineProps<{
  edit: boolean
  isText: boolean
  isVisualization?: boolean
  queryId?: string
  widget?: any
  isEditing?: boolean
  isNew?: boolean
}>()

const emit = defineEmits<{
  (e: 'remove'): void
  (e: 'removeText'): void
  (e: 'toggleTextEdit'): void
  (e: 'editVisualization', payload: { queryId: string; widget: any }): void
}>()

function handleEditVisualization() {
  if (props.queryId && props.widget) {
    emit('editVisualization', {
      queryId: props.queryId,
      widget: props.widget
    })
  }
}
</script>


