<template>
  <form class="space-y-2" data-testid="memory-form" @submit.prevent="emit('save', local)">
    <UTextarea
      v-model="local.text"
      :rows="2"
      :maxlength="MAX"
      autoresize
      :placeholder="$t('profile.memory.form.placeholder')"
      data-testid="memory-form-text"
    />
    <div class="flex items-center gap-2 flex-wrap">
      <UInput v-model="local.tags" size="xs" class="flex-1 min-w-[160px]" :placeholder="$t('profile.memory.form.tags')" data-testid="memory-form-tags" />
      <label class="text-[11px] text-gray-500 flex items-center gap-1">
        <UIcon name="i-heroicons-calendar" class="w-3.5 h-3.5" />
        <UInput v-model="local.date" type="date" size="xs" data-testid="memory-form-date" />
      </label>
      <label v-if="local.date" class="text-[11px] text-gray-500 flex items-center gap-1">
        <span>–</span>
        <UInput v-model="local.end_date" type="date" size="xs" :min="local.date || undefined" data-testid="memory-form-end" />
      </label>
    </div>
    <div class="flex items-center justify-between">
      <span class="text-[10px] text-gray-400">{{ local.text.length }}/{{ MAX }} · {{ $t('profile.memory.form.dateHint') }}</span>
      <div class="flex gap-2">
        <UButton size="2xs" color="gray" variant="ghost" @click="emit('cancel')">{{ $t('common.cancel') }}</UButton>
        <UButton size="2xs" color="blue" type="submit" :loading="saving" :disabled="!local.text.trim()" data-testid="memory-form-save">
          {{ $t('common.save') }}
        </UButton>
      </div>
    </div>
  </form>
</template>

<script setup lang="ts">
export interface MemoryForm {
  mode: 'add' | 'edit'
  id?: string
  text: string
  tags: string
  date: string
  end_date: string
}

const MAX = 280
const props = defineProps<{ form: MemoryForm; saving?: boolean }>()
const emit = defineEmits<{ (e: 'save', form: MemoryForm): void; (e: 'cancel'): void }>()
const local = reactive<MemoryForm>({ ...props.form })
</script>
