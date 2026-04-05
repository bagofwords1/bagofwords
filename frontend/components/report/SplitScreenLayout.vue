<template>

    <div class="flex flex-row h-screen overflow-y-hidden bg-white">
        <!-- Left (Chat) -->
        <div :style="{
                width: isSplitScreen ? `${leftPanelWidth}px` : '100%',
                transform: isSplitScreen ? 'none' : 'translateX(0)',
                willChange: 'transform, width',
                transition: isResizing ? 'none' : 'all 0.2s cubic-bezier(0.4, 0, 0.2, 1)'
             }">
            <slot name="left" />
        </div>

        <!-- Right Panel -->
        <div v-if="isSplitScreen"
             :style="{
                 willChange: 'transform, width',
                 transform: 'translateX(0)',
                 transition: isResizing ? 'none' : 'all 0.2s cubic-bezier(0.4, 0, 0.2, 1)'
             }"
             class="flex-1 min-w-0 relative bg-white flex flex-col">
            <!-- Right header (tabs) -->
            <div class="flex-shrink-0 flex items-center justify-between px-3 pt-1.5">
                <slot name="right-header" />
            </div>
            <!-- Right content (rounded panel) -->
            <div class="flex-1 min-h-0 p-2 pt-1.5 relative">
                <div class="h-full w-full bg-[#f8f8f7] rounded-xl border border-black/[0.08] overflow-hidden">
                    <slot name="right" />
                </div>
                <!-- Resizer overlaid on rounded panel left border -->
                <div class="absolute left-[5px] top-0 bottom-0 w-[8px] cursor-col-resize z-30 group"
                     @mousedown="$emit('startResize', $event)">
                    <div class="absolute inset-y-0 left-[3px] w-[3px] rounded-full opacity-0 group-hover:opacity-100 bg-blue-400 transition-opacity"></div>
                </div>
            </div>
            <!-- Overlay to prevent iframe from capturing mouse events during resize -->
            <div v-if="isResizing" class="absolute inset-0 z-50" />
        </div>
    </div>
</template>

<script setup lang="ts">
const props = defineProps<{ 
    isSplitScreen: boolean,
    leftPanelWidth: number,
    isResizing: boolean,
}>()

defineEmits(['startResize'])
</script>

<style scoped>
.bg-dots {
    background-image: radial-gradient(circle, rgba(0, 0, 0, 0.15) 1px, #fff 1px);
    background-size: 20px 20px;
}
</style>


