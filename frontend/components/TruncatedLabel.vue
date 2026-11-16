<template>
    <!-- A one-line name that may be cut off (a dashboard's title in a card,
         a row, a header). Hovering it shows the whole name -- and the
         description under it, when there is one. No tooltip when the name
         fits and there is nothing more to say.

         The text keeps its own direction (v-text-dir), so a Hebrew name reads
         right to left and its ellipsis lands on the left, its end. On a touch
         screen there is no hover: Nuxt UI hides tooltips on coarse pointers.
         Screen readers already get the full text: the cut is visual only. -->
    <UTooltip
        :prevent="!showTooltip"
        :popper="{ placement: 'bottom-start' }"
        :ui="TOOLTIP_UI"
        class="min-w-0 max-w-full"
    >
        <template #text>
            <div class="flex flex-col gap-0.5 py-1" data-testid="truncated-label-tooltip">
                <span v-text-dir.align class="font-medium text-ink break-words">{{ text }}</span>
                <span
                    v-if="description"
                    v-text-dir.align
                    class="text-ink-muted whitespace-pre-line break-words line-clamp-6"
                >{{ description }}</span>
            </div>
        </template>
        <component
            :is="as"
            ref="label"
            v-text-dir
            v-bind="$attrs"
            class="block min-w-0 truncate"
            @mouseenter="measure"
        >{{ text }}</component>
    </UTooltip>
</template>

<script setup lang="ts">
import type { Component } from 'vue'

defineOptions({ inheritAttrs: false })

const props = withDefaults(defineProps<{
    text: string
    description?: string | null
    // The element or component that holds the text (e.g. a NuxtLink); every
    // other attribute (to, class, listeners) is passed to it.
    as?: string | Component
}>(), { description: null, as: 'span' })

// Nuxt UI's tooltip is one truncated 24px line; this one wraps.
const TOOLTIP_UI = {
    width: 'max-w-sm',
    base: '[@media(pointer:coarse)]:hidden h-auto px-2 py-1 text-xs font-normal relative whitespace-normal',
}

const label = ref<HTMLElement | { $el: HTMLElement } | null>(null)
const overflowing = ref(false)

function measure() {
    const el = label.value && ('$el' in label.value ? label.value.$el : label.value)
    // Measured on hover, not once: the width changes with the window, the
    // sidebar, and the grid's column count.
    overflowing.value = !!el && el.scrollWidth > el.clientWidth + 1
}

const hasDescription = computed(() => !!props.description?.trim())
const showTooltip = computed(() => overflowing.value || hasDescription.value)
</script>
