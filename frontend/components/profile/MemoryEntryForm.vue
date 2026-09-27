<template>
  <form class="space-y-2" data-testid="memory-form" @submit.prevent="emit('save', local)">
    <UTextarea
      v-model="local.text"
      :rows="2"
      :maxlength="MAX"
      autoresize
      :placeholder="$t(`profile.memory.sections.${local.section}.placeholder`)"
      data-testid="memory-form-text"
    />
    <div class="text-[10px] text-gray-400 text-end">{{ local.text.length }}/{{ MAX }}</div>
    <div v-if="local.section === 'events'" class="grid grid-cols-2 gap-2">
      <label class="text-[11px] text-gray-500 space-y-1">
        <span>{{ $t('profile.memory.form.start') }}</span>
        <UInput v-model="local.event_start" type="date" size="xs" required data-testid="memory-form-start" />
      </label>
      <label class="text-[11px] text-gray-500 space-y-1">
        <span>{{ $t('profile.memory.form.end') }}</span>
        <UInput v-model="local.event_end" type="date" size="xs" :min="local.event_start || undefined" data-testid="memory-form-end" />
      </label>
    </div>
    <div class="grid gap-2" :class="showAliases ? 'grid-cols-2' : 'grid-cols-1'">
      <UInput v-model="local.tags" size="xs" :placeholder="$t('profile.memory.form.tags')" data-testid="memory-form-tags" />
      <UInput v-if="showAliases" v-model="local.aliases" size="xs" :placeholder="$t('profile.memory.form.aliases')" data-testid="memory-form-aliases" />
    </div>
    <div class="flex justify-end gap-2">
      <UButton size="2xs" color="gray" variant="ghost" @click="emit('cancel')">{{ $t('common.cancel') }}</UButton>
      <UButton
        size="2xs"
        color="blue"
        type="submit"
        :loading="saving"
        :disabled="!local.text.trim() || (local.section === 'events' && !local.event_start)"
        data-testid="memory-form-save"
      >
        {{ $t('common.save') }}
      </UButton>
    </div>
  </form>
</template>

<script setup lang="ts">
export interface MemoryForm {
  mode: 'add' | 'edit'
  id?: string
  section: 'style' | 'role' | 'vocabulary' | 'events' | 'focus' | 'preferences'
  text: string
  tags: string
  aliases: string
  event_start: string
  event_end: string
}

const MAX = 280
const props = defineProps<{ form: MemoryForm; saving?: boolean }>()
const emit = defineEmits<{ (e: 'save', form: MemoryForm): void; (e: 'cancel'): void }>()
const local = reactive<MemoryForm>({ ...props.form })
const showAliases = computed(() => local.section === 'vocabulary' || local.section === 'focus')
</script>
