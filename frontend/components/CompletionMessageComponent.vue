<template>
    <div>
        <div class="flex rounded-lg p-1"
            :class="{ 'bg-red-50': localCompletion.status === 'error',
             'border border-red-200': localCompletion.status === 'error',
             '-mt-2': localCompletion.role == 'ai_agent',
             'mb-4': localCompletion.role == 'ai_agent' }">

            <div class="w-[28px] mr-2">
                <ChatAvatarComponent :role="localCompletion.role" />
            </div>
            <div class="w-full ml-4">
                <!-- User messages -->
                <div v-if="localCompletion.prompt?.content.length > 0" class="pt-1">
                    <div class="markdown-wrapper">
                        <MDC :value="localCompletion.prompt?.content" class="markdown-content" />
                    </div>
                </div>

                <!-- System messages -->
                <div v-if="localCompletion.role == 'system'">
                    <div class="dots" v-if="localCompletion.completion?.content.length == 0"></div>
                <div v-else>
                    <div class="markdown-wrapper">
                        <MDC :value="localCompletion.completion?.content" class="markdown-content" />
                    </div>


                        <div class="text-xs mt-2 w-full" v-if="localCompletion.widget">
                            <div class="border-2 text-gray-600 bg-white rounded-lg overflow-hidden" :class="{
                                'border-blue-500': isSelected(localCompletion.widget.id, localCompletion.step?.id),
                                'border-gray-200': !isSelected(localCompletion.widget.id, localCompletion.step?.id)
                            }">
                                <div class="p-2 flex justify-between items-center">
                                    <h3 class="text-md font-bold text-gray-600">
                                        {{ localCompletion.widget.title }}
                                        <span v-if="localCompletion.step?.id" class="text-xs font-normal text-gray-400">
                                            Version: {{ localCompletion.step?.id.split('-')[1] }}
                                        </span>
                                    </h3>
                                    <button @click="localCompletion.isCollapsed = !localCompletion.isCollapsed"
                                        class="cursor-pointer text-xs text-gray-400 hover:text-gray-600">
                                        <Icon
                                            :name="localCompletion.isCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" />
                                    </button>
                                </div>
                                <hr />
                                <div v-if="!localCompletion.isCollapsed">
                                    <WidgetTabsComponent :widget="localCompletion.widget"
                                        :step="localCompletion.step" />

                                    <div class="pr-2 pl-2 mt-1.5 pb-1.5 flex justify-between items-center">
                                        <button @click="handleAddClick(localCompletion)"
                                            class="text-xs rounded text-blue-800 hover:text-blue-400"
                                            v-if="localCompletion.step?.status == 'success'">
                                            <Icon name="heroicons-play" />
                                            Add
                                        </button>
                                        <button v-else-if="localCompletion.step?.status == 'error'" class="text-xs rounded text-blue-800">
                                            <Icon name="heroicons-x-mark"
                                                class="w-3 h-3 inline-block" />
                                            {{  localCompletion.step?.status }}
                                        </button>
                                        <button v-else class="text-xs rounded text-blue-800">
                                            <Icon name="heroicons-arrow-path"
                                                class="w-3 h-3 animate-spin inline-block" />
                                            Generating
                                        </button>
                                        <div>
                                            <button class="mr-1.5 text-xs"
                                                @click="selectWidget(localCompletion.widget.id, localCompletion.step?.id, localCompletion.widget.title)">
                                                <Icon name="heroicons-arrow-turn-down-right" />
                                                Follow up
                                                <span
                                                    v-if="isSelected(localCompletion.widget.id, localCompletion.step?.id)">
                                                    (selected)
                                                </span>
                                            </button>
                                            <button @click="openSaveMemoryPopup(localCompletion)" class="text-xs">
                                                <Icon name="heroicons-bookmark" />
                                                Save
                                            </button>
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>



                <!-- AI messages -->
                <div v-else-if="localCompletion.role == 'ai_agent'">
                    <span v-if="localCompletion.role == 'ai_agent'" class="text-green-500 text-xs mr-1 inline">
                        <Icon name="heroicons-cube" 
                            :class="{ 'spin-three-times': true }" />
                        <span class="text-gray-500 ml-2">
                            {{ localCompletion.completion?.content || 'Improving code...' }}
                        </span>
                    </span>
                    <!-- Add Apply to Excel button -->

                </div>
            </div>
        </div>
    </div>

    <!-- Add Save Memory Popup -->
    <div v-if="showSaveMemoryPopup"
        class="z-[1000] fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center">
        <div class="bg-white p-4 rounded-lg shadow-lg">
            <h2 class="text-lg font-bold mb-4">Save Memory</h2>
            <input v-model="memoryTitle" placeholder="Title" class="w-full mb-2 p-2 border rounded">
            <textarea v-model="memoryDescription" placeholder="Description" class="w-full mb-2 p-2 border rounded"
                rows="3"></textarea>
            <div class="flex justify-end text-xs">
                <button @click="cancelSaveMemory" class="mr-2 px-4 py-2 bg-gray-200 rounded">Cancel</button>
                <button @click="saveMemory(localCompletion)"
                    class="px-4 py-2 bg-blue-500 text-white rounded">Save</button>
            </div>
        </div>
    </div>
</template>

<script lang="ts" setup>
import { ref, watch } from 'vue';

const props = defineProps<{
    completion: Object,
    excel: Boolean,
    reportId: string,
    selectedWidgetId: Object
}>()

const emit = defineEmits(['update:selectedWidgetId', 'addWidget']);

function selectWidget(widgetId: string, stepId: string, widgetTitle: string) {
    // If clicking the already selected widget, deselect it
    if (props.selectedWidgetId.widgetId === widgetId && props.selectedWidgetId.stepId === stepId) {
        emit('update:selectedWidgetId', null, null, null);
    } else {
        // Otherwise, select the new widget
        emit('update:selectedWidgetId', widgetId, stepId, widgetTitle);
    }
}

function isSelected(widgetId: string, stepId: string) {
    return props.selectedWidgetId.widgetId === widgetId && props.selectedWidgetId.stepId === stepId;
}

const localCompletion = computed(() => ({
    ...props.completion,
}));

const showSaveMemoryPopup = ref(false);
const memoryTitle = ref('');
const memoryDescription = ref('');

const openSaveMemoryPopup = (completion: any) => {
    showSaveMemoryPopup.value = true;
    memoryTitle.value = completion.widget?.title || '';
    memoryDescription.value = '';
}

const cancelSaveMemory = () => {
    showSaveMemoryPopup.value = false;
    memoryTitle.value = '';
    memoryDescription.value = '';
}

const saveMemory = async (completion: any) => {
    try {
        const response = await useMyFetch('/api/memories', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                title: memoryTitle.value,
                description: memoryDescription.value,
                step_id: completion.step.id,
                report_id: completion.report_id,
                widget_id: completion.widget.id
            }),
        }).then(response => {
            if (response.status.value === 'success') {
                console.log('Memory saved successfully');
                showSaveMemoryPopup.value = false;
                memoryTitle.value = '';
                memoryDescription.value = '';
            } else {
                console.error('Failed to save memory');
            }
        });

        console.log('Memory saved successfully');
        showSaveMemoryPopup.value = false;
        memoryTitle.value = '';
        memoryDescription.value = '';
    } catch (error) {
        console.error('Error saving memory:', error);
    }
}

const handleAddClick = (completion: any) => {
    if (props.excel) {
        // Existing Excel functionality
        const serializedData = JSON.stringify(completion);
        window.parent.postMessage({
            type: 'applyToExcel',
            data: serializedData
        }, '*');
    } else {
        // First update the widget status to published
        emit('addWidget', {
            ...completion.widget,
            step: completion.step  // Include the step data
        });
        // Then select the widget
        selectWidget(completion.widget.id, completion.step_id, completion.widget.title);
    }
}

// Watch for changes in selectedWidgetId to debug
watch(props.selectedWidgetId, (newVal) => {
    //console.log('selectedWidgetId changed:', newVal);
});

const activeTab = ref('model');

// Update the watch function
watch(() => localCompletion.value?.step?.data_model?.type, (newType) => {
    if (newType === 'pie_chart' || newType === 'line_chart' || newType === 'bar_chart' || newType === 'count') {
        activeTab.value = 'visual';
    }
}, { immediate: true });

</script>

<style scoped>
@keyframes shimmer {
    0% {
        background-position: -100% 0;
    }

    100% {
        background-position: 100% 0;
    }
}

@keyframes ellipsis {
    0% {
        content: 'Thinking.';
    }

    33% {
        content: 'Thinking..';
    }

    66% {
        content: 'Thinking...';
    }
}

.dots::after {
    content: 'Thinking...';
    display: inline-block;
    margin-top: 5px;
    background: linear-gradient(90deg,
            #888 0%,
            #999 25%,
            #ccc 50%,
            #999 75%,
            #888 100%);
    background-size: 200% 100%;
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
    animation:
        shimmer 2s linear infinite,
        ellipsis 1s infinite,
        fadeInOut 0.5s ease-in-out;
    font-weight: 400;
    font-size: 14px;
    opacity: 1;
}

@keyframes fadeInOut {
    0% {
        opacity: 0;
    }

    100% {
        opacity: 1;
    }
}

@keyframes spin-three-times {
    0% { transform: rotate(0deg); }
    100% { transform: rotate(1080deg); } /* 360 * 3 = 1080 degrees */
}

.spin-three-times {
    animation: spin-three-times 1.5s ease-in-out forwards;
}

ol,
ul {
    @apply list-none;
}

.markdown-wrapper :deep(.markdown-content) {
    /* Basic text styling */
    @apply text-gray-700 leading-relaxed;
    font-size: 14px;

    /* Headers */
    :where(h1, h2, h3, h4, h5, h6) {
        @apply font-bold mb-4 mt-6;
    }

    h1 {
        @apply text-3xl;
    }

    h2 {
        @apply text-2xl;
    }

    h3 {
        @apply text-xl;
    }

    /* Lists */
    ul,
    ol {
        @apply pl-6 mb-4;
    }

    ul {
        @apply list-disc;
    }

    ol {
        @apply list-decimal;
    }

    li {
        @apply mb-1.5;
    }

    /* Code blocks */
    pre {
        @apply bg-gray-50 p-4 rounded-lg mb-4 overflow-x-auto;
    }

    code {
        @apply bg-gray-50 px-1 py-0.5 rounded text-sm font-mono;
    }

    /* Links */
    a {
        @apply text-blue-600 hover:text-blue-800 underline;
    }

    /* Block quotes */
    blockquote {
        @apply border-l-4 border-gray-200 pl-4 italic my-4;
    }

    /* Tables */
    table {
        @apply w-full border-collapse mb-4;

        th,
        td {
            @apply border border-gray-200 p-2;
            @apply text-xs;
            @apply p-1.5;
            @apply bg-white;
        }

        th {
            @apply bg-gray-50;
            @apply p-1.5;
            @apply text-xs;
        }
    }

    /* Paragraphs and spacing */
    p {
        @apply mb-4;
    }

    /* Images */
    img {
        @apply max-w-full h-auto rounded-lg;
    }
}
</style>
