<template>
    <!-- Add tabs -->
    <div class="flex border-b border-gray-200 mb-2">
        <button
            v-if="props.step?.data_model?.type === 'pie_chart' || props.step?.data_model?.type === 'line_chart' || props.step?.data_model?.type === 'bar_chart' || props.step?.data_model?.type === 'count'"
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
        <div v-if="activeTab === 'visual'" class="bg-gray-50 rounded p-4 text-xs">
            <div class="text-xs">
                <div v-if="props.step?.data_model?.type === 'pie_chart'">
                    <RenderVisual :widget="props.widget" :data="props.step?.data" :data_model="props.step?.data_model" />
                </div>
                <div v-else-if="props.step?.data_model?.type === 'line_chart'">
                    <RenderVisual :widget="props.widget" :data="props.step?.data" :data_model="props.step?.data_model" />
                </div>
                <div v-else-if="props.step?.data_model?.type === 'bar_chart'">
                    <RenderVisual :widget="props.widget" :data="props.step?.data" :data_model="props.step?.data_model" />
                </div>
                <div v-else-if="props.step?.data_model?.type === 'count'">
                    <RenderCount :show_title="true" :widget="props.widget" :data="props.step?.data"
                        :data_model="props.step?.data_model" />
                </div>
            </div>
        </div>
    </Transition>

    <!-- Data Model Table -->
    <Transition name="fade" mode="out-in">
        <div v-if="activeTab === 'model'">
            <transition-group tag="table" name="fade" class="border-collapse w-full">
                <tr v-for="column in props.step?.data_model.columns" :key="column.generated_column_name">
                    <th class="border-t border-b border-r border-gray-200 px-2 py-1">
                        {{ column.generated_column_name }}
                    </th>
                    <td class="border-t border-b border-l border-gray-200 px-2 py-1">
                        {{ column.description }}
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
import hljs from 'highlight.js';
import 'highlight.js/styles/github.css'; // You can choose different styles

const props = defineProps<{
    widget: any,
    step: any,
}>()

// Set default tab based on data_model type:
// - 'visual' for any chart type (pie_chart, line_chart, bar_chart)
// - 'count' for count type
// - 'model' for table type (default)
const activeTab = ref(
    props.step?.data_model?.type?.includes('chart') ? 'visual' :
    props.step?.data_model?.type === 'count' ? 'visual' :
    'model'
)

// Add computed property for highlighted code
const highlightedCode = computed(() => {
    if (!props.step?.code) return '';
    // Assuming SQL - adjust if needed
    return hljs.highlight(props.step.code, { language: 'python' }).value;
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