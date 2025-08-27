<template>

    <header class="sticky top-0 bg-white z-10 flex flex-row pt-1 h-[40px] border-gray-200 pb-1 pr-2 items-center">
        <GoBackChevron />
        <h1 class="text-sm md:text-left text-center mt-1">
            <span class="font-semibold text-sm">{{ report?.title || 'Chat' }}</span>
        </h1>
        <div class="ml-auto flex items-center gap-1">
            <button v-if="isStreaming" class="px-2 py-1 text-xs border rounded" @click="$emit('stop')">Stop</button>
            <button @click="$emit('toggleSplitScreen')" class="p-1.5 rounded text-xl hover:bg-gray-100 flex items-center">
                <span class="inline-flex items-center">
                    <Icon name="heroicons:chart-pie" class="inline-block mr-2" />
                </span>
                <span class="text-sm" :class="isSplitScreen ? 'hidden' : 'inline'">Dashboard</span>
            </button>
        </div>
    </header>
</template>

<script setup lang="ts">
import GoBackChevron from '@/components/excel/GoBackChevron.vue'
import ShareModal from '@/components/ShareModal.vue'

defineProps<{ 
    report: any | null,
    isSplitScreen: boolean,
    isStreaming: boolean,
}>()

defineEmits(['toggleSplitScreen', 'rerun', 'stop'])
</script>


