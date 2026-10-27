<template>
    <!-- Reader controls for the conversation surface: measure and text size.
         Both are personal and stored per browser (see useChatDisplay), so
         this needs no permission check — unlike /settings/general, which
         sets organization-wide values behind `manage_settings`. -->
    <UPopover :popper="{ placement: 'bottom-end', strategy: 'fixed' }">
        <UTooltip :text="t('reportView.display.title')" :popper="{ strategy: 'fixed', placement: 'bottom' }">
            <button
                type="button"
                data-testid="chat-display-button"
                :aria-label="t('reportView.display.title')"
                class="hidden md:flex p-1.5 rounded hover:bg-gray-100 dark:hover:bg-gray-700 items-center"
            >
                <Icon name="heroicons:adjustments-horizontal" class="w-5 h-5 text-gray-500 dark:text-gray-400" />
            </button>
        </UTooltip>

        <template #panel>
            <div class="p-3 w-[232px] text-xs" data-testid="chat-display-panel">
                <!-- Text size -->
                <div class="flex items-center justify-between mb-1.5">
                    <span class="font-medium text-ink-2">{{ t('reportView.display.textSize') }}</span>
                    <span class="text-ink-muted nums">{{ t('reportView.display.pixels', { n: textSizePx }) }}</span>
                </div>
                <div
                    role="radiogroup"
                    :aria-label="t('reportView.display.textSize')"
                    class="flex items-stretch gap-1 mb-3"
                >
                    <button
                        v-for="(scale, i) in scales"
                        :key="scale"
                        type="button"
                        role="radio"
                        :aria-checked="textScale === scale"
                        :data-testid="`chat-text-scale-${scale}`"
                        :title="t(`reportView.display.scales.${SCALE_LABELS[i]}`)"
                        class="flex-1 rounded border py-1.5 transition-colors"
                        :class="textScale === scale
                            ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-300'
                            : 'border-line text-ink-muted hover:bg-gray-50 dark:hover:bg-gray-800/50'"
                        @click="setTextScale(scale)"
                    >
                        <!-- The control previews what it does: each step
                             renders its own "A" at the size it would set,
                             capped so the row does not change height. -->
                        <span :style="{ fontSize: `${Math.round(13 * scale)}px`, lineHeight: '1' }">A</span>
                    </button>
                </div>

                <!-- Measure -->
                <div class="font-medium text-ink-2 mb-1.5">{{ t('reportView.display.width') }}</div>
                <div role="radiogroup" :aria-label="t('reportView.display.width')" class="flex items-stretch gap-1">
                    <button
                        v-for="option in WIDTH_OPTIONS"
                        :key="option"
                        type="button"
                        role="radio"
                        :aria-checked="width === option"
                        :data-testid="`chat-width-${option}`"
                        class="flex-1 flex items-center justify-center gap-1.5 rounded border py-1.5 transition-colors"
                        :class="width === option
                            ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-300'
                            : 'border-line text-ink-muted hover:bg-gray-50 dark:hover:bg-gray-800/50'"
                        @click="setWidth(option)"
                    >
                        <Icon
                            :name="option === 'standard' ? 'heroicons:list-bullet' : 'heroicons:arrows-pointing-out'"
                            class="w-3.5 h-3.5 shrink-0"
                        />
                        <span>{{ t(`reportView.display.widths.${option}`) }}</span>
                    </button>
                </div>

                <p class="mt-3 text-2xs text-ink-muted leading-normal">
                    {{ t('reportView.display.scopeHint') }}
                </p>
            </div>
        </template>
    </UPopover>
</template>

<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import type { ChatWidth } from '~/composables/useChatDisplay'

const { t } = useI18n()
const { width, textScale, textSizePx, scales, setWidth, setTextScale } = useChatDisplay()

const WIDTH_OPTIONS: ChatWidth[] = ['standard', 'wide']

/* Parallel to CHAT_TEXT_SCALES — the scales are numbers, the catalog needs
   stable key names, and `scales.0.875` is not a usable i18n path. */
const SCALE_LABELS = ['small', 'default', 'large', 'larger'] as const
</script>
