<template>
    <!-- One button for every export, everywhere. It stays a menu even with a
         single option so the interaction never changes shape as formats come
         and go: the same click always opens the same list. -->
    <UDropdown
        v-if="options.length"
        :items="[menuItems]"
        :popper="{ placement: 'bottom-end' }"
        :disabled="busy"
        :ui="{ width: 'w-auto min-w-[8rem]', item: { padding: 'px-2 py-1.5' } }"
    >
        <UTooltip :text="$t('exports.label')">
            <button
                type="button"
                :disabled="busy"
                :aria-label="$t('exports.label')"
                class="inline-flex items-center gap-1 rounded px-2 py-1 transition-colors disabled:opacity-50"
                :class="variant === 'subtle'
                    ? 'text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-300'
                    : 'hover:bg-gray-100 dark:hover:bg-gray-700 text-gray-500 dark:text-gray-400'"
            >
                <UIcon
                    :name="busy ? 'i-heroicons-arrow-path' : 'i-heroicons-arrow-down-tray'"
                    class="w-3.5 h-3.5"
                    :class="busy ? 'animate-spin' : ''"
                />
            </button>
        </UTooltip>

        <template #item="{ item }">
            <div class="flex items-center gap-2 text-start w-full">
                <UIcon :name="item.icon" class="w-4 h-4 shrink-0 text-gray-400 dark:text-gray-500" />
                <span class="text-xs font-medium text-gray-900 dark:text-white">{{ item.label }}</span>
            </div>
        </template>
    </UDropdown>
</template>

<script setup lang="ts">
import type { ExportFormat, ExportOption } from '~/composables/useArtifactExports'

const props = withDefaults(
    defineProps<{
        options: ExportOption[]
        busy?: boolean
        /** 'subtle' matches the muted top bar on the public share page. */
        variant?: 'toolbar' | 'subtle'
    }>(),
    { busy: false, variant: 'toolbar' },
)

const emit = defineEmits<{ (e: 'select', format: ExportFormat): void }>()

const menuItems = computed(() =>
    props.options.map(option => ({
        label: option.label,
        icon: option.icon,
        click: () => emit('select', option.format),
    })),
)
</script>
