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

        <!-- Resizer -->
        <div v-if="isSplitScreen" 
             class="w-1 bg-gray-200 cursor-col-resize hover:bg-blue-500 active:bg-blue-700 z-20"
             @mousedown="$emit('startResize', $event)">
        </div>

        <!-- Right (Dashboard) -->
        <div v-if="isSplitScreen" 
             :style="{ 
                 width: `calc(100% - ${leftPanelWidth}px)`,
                 willChange: 'transform, width',
                 transform: 'translateX(0)',
                 transition: isResizing ? 'none' : 'all 0.2s cubic-bezier(0.4, 0, 0.2, 1)'
             }"
             :class="[
                'bg-white border-gray-200 bg-dots',
                'overflow-y-scroll'
             ]">
            <slot name="right" />
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


