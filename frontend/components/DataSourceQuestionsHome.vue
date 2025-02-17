<template>
    <UDropdown :items="getConversationStarters(data_source)" :ui="{ width: 'w-81' }" v-for="data_source in props.data_sources">
        <button
            class="group relative overflow-hidden rounded bg-white px-4 py-2 text-xs text-gray-500 transition-all duration-300 ease-out hover:bg-gradient-to-r hover:from-gray-50 hover:to-gray-100 ring-1 ring-gray-200 hover:ring-1  hover:ring-offset-2">
            <span
                class="ease absolute right-0 -mt-12 h-32 w-8 translate-x-12 rotate-12 transform bg-white opacity-10 transition-all duration-700 group-hover:-translate-x-40"></span>
            <div class="relative">
                <DataSourceIcon :type="data_source.type" class="h-3 inline mr-2" />
                <transition name="fade" mode="out-in">
                    <span :key="starterIndexes.get(data_source.id)">
                        {{ getCurrentStarter(data_source) }}
                    </span>
                </transition>
                <span>
                    <UIcon name="i-heroicons-chevron-down" class="h-3 inline-block ml-2" />
                </span>
            </div>
        </button>
        <template #item="{ item }">
            <div @click="emitContent(item.value)" class="text-left text-sm">
                {{ item.label }}
            </div>
        </template>
    </UDropdown>


    <!-- excel / PDF -->
    <button @click="emitContent('Extract data from Excel / PDF')"
        class="group relative overflow-hidden rounded bg-white px-4 py-2 text-xs text-gray-500 transition-all duration-300 ease-out hover:bg-gradient-to-r hover:from-gray-50 hover:to-gray-100 ring-1 ring-gray-200 hover:ring-1  hover:ring-offset-2">
        <span
            class="ease absolute right-0 -mt-12 h-32 w-8 translate-x-12 rotate-12 transform bg-white opacity-10 transition-all duration-700 group-hover:-translate-x-40"></span>
        <span class="relative">
            <DataSourceIcon type="excel" class="h-3 inline mr-2" />
            Extract data from Excel / PDF
        </span>
    </button>
</template>

<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'

const props = defineProps<{
    data_sources: any[]
}>()

console.log('Component initializing, data_sources:', props.data_sources)

const starterIndexes = ref(new Map())
const emit = defineEmits(['update-content'])

// Watch for changes in data_sources
watch(() => props.data_sources, (newDataSources) => {
    if (!newDataSources || !Array.isArray(newDataSources) || newDataSources.length === 0) {
        console.warn('Invalid or empty data_sources')
        return
    }

    
    newDataSources.forEach(ds => {
        if (!ds.id || !ds.conversation_starters) {
            console.warn('Invalid data source:', ds)
            return
        }

        starterIndexes.value.set(ds.id, 0)
        
        const randomInterval = 35000 + Math.random() * 5000
        
        setInterval(() => {
            const currentIndex = starterIndexes.value.get(ds.id)
            const maxIndex = ds.conversation_starters?.length || 1
            const newMap = new Map(starterIndexes.value)
            newMap.set(ds.id, (currentIndex + 1) % maxIndex)
            starterIndexes.value = newMap
        }, randomInterval)
    })
}, { immediate: true })

function getConversationStarters(data_source: any) {
    return [data_source.conversation_starters.map((item: any) => ({
        label: item.split('\n')[0],
        icon: data_source.type,
        value: item
    }))]
}

function getCurrentStarter(data_source: any) {
    if (!data_source.conversation_starters?.length) return ''
    const index = starterIndexes.value.get(data_source.id) || 0
    return data_source.conversation_starters[index].split('\n')[0]
}

function emitContent(content: string) {
    emit('update-content', content)
}

</script>

<style scoped>
.fade-enter-active,
.fade-leave-active {
    transition: opacity 0.5s ease;
}

.fade-enter-from,
.fade-leave-to {
    opacity: 0;
}
</style>