<template>
    <!-- Add tabs -->
    <div class="flex border-b border-gray-200 mb-2">
        <!-- Simplified v-if for Visual tab button -->
        <button
            v-if="isVisualType(props.step?.data_model?.type)"
            @click="activeTab = 'visual'" class="px-4 py-1 text-xs"
            :class="{ 'border-b-2 border-blue-500 text-blue-600': activeTab === 'visual', 'text-gray-500': activeTab !== 'visual' }">
            Visual
        </button>
        <button @click="activeTab = 'model'" class="px-4 py-1 text-xs"
            :class="{ 'border-b-2 border-blue-500 text-blue-600': activeTab === 'model', 'text-gray-500': activeTab !== 'model' }">
            Data Model
        </button>
        <button @click="activeTab = 'data'" class="px-4 py-1 text-xs"
            :class="{ 'border-b-2 border-blue-500 text-blue-600': activeTab === 'data', 'text-gray-500': activeTab !== 'data' }">
            Data
            <span v-if="!props.step?.data?.rows" class="text-xs text-gray-400">
                <span class="inline-block animate-pulse">•</span>
                <span class="inline-block animate-pulse delay-100">•</span>
                <span class="inline-block animate-pulse delay-200">•</span>
            </span>
        </button>
        <button @click="activeTab = 'code'" class="px-4 py-1 text-xs"
            :class="{ 'border-b-2 border-blue-500 text-blue-600': activeTab === 'code', 'text-gray-500': activeTab !== 'code' }">
            Code
        </button>

    </div>

    <!-- Visual -->
    <Transition name="fade" mode="out-in">
        <!-- Simplified Visual tab content -->
        <div v-if="activeTab === 'visual'" class="bg-gray-50 rounded p-4 text-xs">
             <!-- Check if it's a chart type handled by RenderVisual -->
             <div v-if="chartVisualTypes.has(props.step?.data_model?.type)" class="h-[400px]">
                  <RenderVisual :widget="props.widget" :data="props.step?.data" :data_model="props.step?.data_model" />
             </div>
             <!-- Handle the count type separately -->
             <div v-else-if="props.step?.data_model?.type === 'count'">
                  <RenderCount :widget="props.widget" :data="props.step?.data" :data_model="props.step?.data_model" />
             </div>
             <!-- Optional: Add a fallback for unexpected types -->
             <div v-else>
                 Unknown visual type: {{ props.step?.data_model?.type }}
             </div>
         </div>
    </Transition>

    <!-- Data Model Table -->
    <Transition name="fade" mode="out-in">
        <div v-if="activeTab === 'model'">
            <!-- Keep existing data model table -->
            <transition-group tag="table" name="fade" class="border-collapse w-full">
                <tr v-for="column in props.step?.data_model?.columns" :key="column.generated_column_name">
                    <th class="border-t border-b border-r border-gray-200 px-2 py-1">
                        {{ column.generated_column_name }}
                    </th>
                    <td class="border-t border-b border-l border-gray-200 px-2 py-1">
                        {{ column.description }}
                        <UTooltip :text="column.source">
                            <Icon name="heroicons:information-circle" class="text-gray-500" />
                        </UTooltip>
                    </td>
                </tr>
            </transition-group>
        </div>
    </Transition>

    <!-- Data Table -->
    <Transition name="fade" mode="out-in">
        <div v-if="activeTab === 'data'" class="h-[500px]">
            <RenderTable :widget="props.widget" :step="props.step" />
        </div>
    </Transition>

    <!-- Code View -->
    <Transition name="fade" mode="out-in">
        <div v-if="activeTab === 'code'" class="bg-gray-50 rounded p-4 text-xs">
            <pre><code class="hljs" v-html="highlightedCode"></code></pre>
        </div>
    </Transition>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'; // Import computed
import hljs from 'highlight.js';
import 'highlight.js/styles/github.css'; // You can choose different styles

// Import your visual components (ensure paths are correct)
import RenderVisual from './RenderVisual.vue';
import RenderCount from './RenderCount.vue';
import RenderTable from './RenderTable.vue';
// Assuming UTooltip and Icon are globally registered or imported elsewhere

const props = defineProps<{
    widget: any,
    step: any,
}>()

// Define the set of types handled by RenderVisual
const chartVisualTypes = new Set([
    'pie_chart',
    'line_chart',
    'bar_chart',
    'area_chart',
    'heatmap',
    'scatter_plot',
    'map',
    'candlestick',
    'treemap',
    'radar_chart'
]);

// Helper function to determine if the type should show the Visual tab
function isVisualType(type: string | undefined): boolean {
    if (!type) return false;
    return chartVisualTypes.has(type) || type === 'count';
}

// Simplified default active tab logic
const activeTab = ref(
    isVisualType(props.step?.data_model?.type) ? 'visual' : 'model' // Default to model if not visual
);

// Add computed property for highlighted code
const highlightedCode = computed(() => {
    if (!props.step?.code) return '';
    // Assuming SQL - adjust if needed
    // Ensure 'sql' is the correct language identifier for highlight.js
    try {
        return hljs.highlight(props.step.code, { language: 'sql', ignoreIllegals: true }).value;
    } catch (e) {
        console.error("Highlighting error:", e);
        return props.step.code; // Return plain code on error
    }
});

</script>

<style scoped>
.fade-enter-active,
.fade-leave-active {
    transition: opacity 0.2s ease;
}

.fade-enter-from,
.fade-leave-to {
    opacity: 0;
}
</style>