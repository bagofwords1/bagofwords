<template>
  <!-- A collapsible settings block: a header row (title, one line of help,
       optional status) that opens into its body. Stack several inside a
       `.settings-disclosure-group` to get one bordered list with hairlines
       between rows, as on AI Settings and Identity Provider. -->
  <div class="settings-disclosure">
    <button
      type="button"
      class="w-full flex items-center justify-between gap-4 px-5 py-4 text-start hover:bg-ink/[0.03] transition-colors"
      :aria-expanded="isOpen"
      @click="toggle"
    >
      <div class="min-w-0">
        <div class="flex items-center gap-2 text-sm font-semibold text-ink">
          {{ title }}
          <slot name="badge" />
        </div>
        <p v-if="description" class="mt-0.5 text-sm text-ink-muted truncate">{{ description }}</p>
      </div>
      <div class="flex items-center gap-3 shrink-0">
        <span v-if="$slots.status && !isOpen" class="hidden sm:inline text-xs text-ink-muted"><slot name="status" /></span>
        <UIcon name="i-heroicons-chevron-down" :class="['w-4 h-4 text-ink-muted transition-transform duration-fast', isOpen ? 'rotate-180' : '']" />
      </div>
    </button>
    <div v-show="isOpen" class="px-5 pb-6 pt-2">
      <slot />
    </div>
  </div>
</template>

<script setup lang="ts">
const props = defineProps<{
  title: string
  description?: string
  defaultOpen?: boolean
}>()

const open = defineModel<boolean | undefined>('open', { default: undefined })
const internal = ref(!!props.defaultOpen)
const isOpen = computed(() => open.value ?? internal.value)
const toggle = () => {
  if (open.value !== undefined) open.value = !open.value
  else internal.value = !internal.value
}
</script>
