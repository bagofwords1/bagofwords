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
                    <div class="inline float-right" v-if="useCan('view_completion_plan')">
                        <button @click="showPlan(localCompletion)" 
                                class="text-xs px-2 py-1 bg-gray-100 hover:bg-gray-200 rounded flex items-center">
                            <Icon name="heroicons-eye" class="mr-1" />
                        </button>
                    </div>
                    <div class="markdown-wrapper">

                        <MDC :value="localCompletion.prompt?.content" class="markdown-content" />

                    </div>
                </div>
                <!-- System messages -->
                <div v-if="localCompletion.role == 'system'">

                    <!-- Show thinking dots when reasoning is empty -->
                    <div v-if="(!localCompletion.completion?.reasoning || localCompletion.completion?.reasoning.length == 0) && !localCompletion.completion?.content">
                        <div class="simple-dots"></div>
                    </div>
                    <!-- Collapsible reasoning section -->
                        <div v-if="localCompletion.completion?.reasoning && localCompletion.completion?.reasoning.length > 0">
                            <div class="flex justify-between items-center cursor-pointer" 
                                @click="reasoningCollapsed = !reasoningCollapsed">
                            <div class="font-medium text-sm text-gray-400 mb-2">
                                <!-- Always show "Thought Process" when content is available -->
                                <div class="flex items-center">
                                    <Icon :name="reasoningCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" 
                                         class="w-4 h-4 text-gray-500" />
                                    <span v-if="(localCompletion.completion?.content && localCompletion.completion?.content.length > 0) || localCompletion.status === 'stopped' || localCompletion.status === 'error' || localCompletion.sigkill" class="ml-1">
                                        Thought Process
                                    </span>
                                    <!-- Show "Thinking" when no content is available -->
                                    <span v-else class="ml-1">
                                        <div class="dots" />
                                    </span>
                                </div>
                            </div>
                            
                        </div>
                        <Transition name="fade">
                            <div v-if="!reasoningCollapsed" 
                                 class="text-sm mt-2 leading-relaxed text-gray-500 mb-3 reasoning-content">
                                <MDC :value="localCompletion.completion?.reasoning" class="markdown-content" />
                            </div>
                        </Transition>
                    </div>
                    
                    <!-- Always show content when available -->
                    <div v-if="localCompletion.completion?.content" class="markdown-wrapper">
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
                                <div class="flex items-center">
                                    <UTooltip text="Download CSV" v-if="localCompletion.step?.id && localCompletion.step?.status === 'success'">
                                    <button @click="downloadStepCSV(localCompletion.step?.id, localCompletion.widget?.title, localCompletion.step?.slug)"
                                        v-if="localCompletion.step?.id && localCompletion.step?.status === 'success'"
                                        class="cursor-pointer text-xs text-gray-400 hover:text-gray-600 mr-2"
                                        title="Download CSV">
                                        <Icon name="heroicons-arrow-down-tray" />
                                    </button>
                                </UTooltip> 
                                    <button @click="localCompletion.isCollapsed = !localCompletion.isCollapsed"
                                        class="cursor-pointer text-xs text-gray-400 hover:text-gray-600">
                                        <Icon
                                            :name="localCompletion.isCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" />
                                    </button>
                                </div>
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
                                    <button v-else-if="localCompletion.status === 'stopped' || localCompletion.sigkill" class="text-xs rounded text-blue-800">
                                        <Icon name="heroicons-stop-circle" />
                                        Stopped generating
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
                        <div class="flex mt-3">
                                <CompletionItemFeedback 
                                    v-if="localCompletion.status === 'success' || localCompletion.status === 'error'"
                                    :completion="localCompletion" 
                                    :feedbackScore="localCompletion.feedback_score || 0" 
                                />
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

    <!-- Add Plan Modal -->
    <UModal v-model="showPlanModal" :ui="{ width: 'max-w-3xl' }">
        <div class="p-4">
            <div class="flex justify-between items-center mb-4">
                <h2 class="text-lg font-bold">X-ray View</h2>
                <button @click="showPlanModal = false" class="text-gray-500 hover:text-gray-700">
                    <Icon name="heroicons-x-mark" class="w-5 h-5" />
                </button>
            </div>

                <hr class="w-full mb-4 mt-4" />
            <div v-if="planLoading" class="flex justify-center items-center py-8">
                <Icon name="heroicons-arrow-path" class="w-6 h-6 animate-spin text-blue-500" />
            </div>
            <div v-else-if="plans.length > 0" class="markdown-wrapper max-h-[60vh] overflow-auto">
                <!-- Plans Navigation -->
                <div class="mb-4 flex space-x-2">
                    <button 
                        v-for="(p, index) in plans" 
                        :key="index"
                        @click="switchPlan(index)"
                        class="px-3 py-1 rounded text-sm"
                        :class="{
                            'bg-blue-500 text-white': activePlanIndex === index,
                            'bg-gray-100 hover:bg-gray-200': activePlanIndex !== index
                        }"
                    >
                        Plan {{ index + 1 }}
                    </button>
                </div>

                <!-- Plan Content -->
                <div v-if="plan">
                    <div class="flex justify-between items-center cursor-pointer" @click="togglePromptCollapsed">
                        <h3 class="text-md font-bold mb-2">Prompt</h3>
                        <Icon :name="promptCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-5 h-5" />
                    </div>
                    <pre v-if="!promptCollapsed" class="text-xs">{{ plan_content.text }}</pre>
                    
                    <div class="flex justify-between items-center cursor-pointer mt-4" @click="togglePlanCollapsed">
                        <h3 class="text-md font-bold mb-2">Plan</h3>
                        <Icon :name="planCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-5 h-5" />
                    </div>
                    <pre v-if="!planCollapsed" class="text-xs">
                        <div class="text-xs">Analysis Complete: {{ plan_analysis_complete }}</div>
                        <div class="text-xs">Reasoning: {{ plan_reasoning }}</div>
                        <div class="text-xs">{{ plan_content.plan }}</div>
                    </pre>

                    <!-- Add token usage section -->
                    <div class="flex justify-between items-center cursor-pointer mt-4" @click="toggleTokensCollapsed">
                        <h3 class="text-md font-bold mb-2">Token Usage</h3>
                        <Icon :name="tokensCollapsed ? 'heroicons-chevron-right' : 'heroicons-chevron-down'" class="w-5 h-5" />
                    </div>
                    <div v-if="!tokensCollapsed" class="text-xs bg-gray-50 p-3 rounded">
                        <div v-if="plan_content.token_usage">
                            <div class="grid grid-cols-2 gap-2">
                                <div>Prompt Tokens:</div>
                                <div class="font-mono">{{ plan_content.token_usage.prompt_tokens }}</div>
                                <div>Completion Tokens:</div>
                                <div class="font-mono">{{ plan_content.token_usage.completion_tokens }}</div>
                                <div class="font-bold">Total Tokens:</div>
                                <div class="font-mono font-bold">{{ plan_content.token_usage.total_tokens }}</div>
                            </div>
                        </div>
                        <div v-else class="text-gray-500">
                            Token usage information not available
                        </div>
                    </div>
                </div>
            </div>
            <div v-else class="text-gray-500 py-4 text-center">
                No plan information available
            </div>
        </div>
    </UModal>

    <!-- Add Instruction Modal -->
    <InstructionModalComponent
        v-model="showInstructionModal"
        :instruction="null"
        @instructionSaved="handleInstructionSaved"
    />

</template>

<script lang="ts" setup>
import { ref, watch, computed } from 'vue';
import { useCan } from '~/composables/usePermissions';
import InstructionModalComponent from '~/components/InstructionModalComponent.vue';

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

const plan = ref(null);
const plan_content = ref(null);
const plan_reasoning = ref(null);
const plan_analysis_complete = ref(null);

const localCompletion = computed(() => ({
    ...props.completion,
}));

const showSaveMemoryPopup = ref(false);
const memoryTitle = ref('');
const memoryDescription = ref('');

const reasoningCollapsed = ref(false);

// Add this with your other watchers
watch(() => localCompletion.value?.completion?.content, (newContent) => {
    // Collapse reasoning when content becomes available
    if (newContent && newContent.length > 0) {
        reasoningCollapsed.value = true;
    } else {
        reasoningCollapsed.value = false;
    }
}, { immediate: true });

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

const downloadStepCSV = async (stepId, widgetTitle, stepSlug) => {
    if (!stepId) return;
    try {
        // Use `useMyFetch` with the correct `responseType` option
        const { data, error } = await useMyFetch(`/api/steps/${stepId}/export`, {
            responseType: 'blob'
        });

        // Check the error ref for any issues
        if (error.value) {
            // The error object from useFetch contains more details
            throw new Error(`Download failed: ${error.value.message || 'Unknown error'}`);
        }

        // The data ref will now correctly hold the Blob object
        const blob = data.value;
        if (!blob) {
            throw new Error('No data received from server.');
        }

        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.style.display = 'none';
        a.href = url;
        
        // Sanitize and create a more human-readable filename
        const safeWidgetTitle = widgetTitle?.replace(/[^a-zA-Z0-9_-\s]/g, '').trim() || 'widget';
        const fileName = `${safeWidgetTitle.replace(/\s+/g, '_')}-${stepSlug || stepId}.csv`;
        a.download = fileName;

        document.body.appendChild(a);
        a.click();
        window.URL.revokeObjectURL(url);
        a.remove();
    } catch (error) {
        console.error('Error downloading the CSV:', error);
        // Here you could trigger a user-facing notification about the error.
    }
};

const showPlanModal = ref(false);
const planLoading = ref(false);
const promptCollapsed = ref(false);
const planCollapsed = ref(false);
const tokensCollapsed = ref(false);


const togglePromptCollapsed = () => {
    promptCollapsed.value = !promptCollapsed.value;
}

const togglePlanCollapsed = () => {
    planCollapsed.value = !planCollapsed.value;
}

const toggleTokensCollapsed = () => {
    tokensCollapsed.value = !tokensCollapsed.value;
}

const showPlan = async (completion: any) => {
    showPlanModal.value = true;
    planLoading.value = true;
    
    try {
        if (completion.id) {
            await getPlan(completion.id);
        } else {
            console.error('Completion ID not available');
            plan.value = { content: "Plan information not available" };
        }
    } catch (error) {
        console.error('Error fetching plan:', error);
        plan.value = { content: "Error loading plan information" };
    } finally {
        planLoading.value = false;
    }
}

const isStoppingGeneration = ref(false);


const getPlan = async (completionId: string) => {
    try {
        const response = await useMyFetch(`/api/completions/${completionId}/plans`);
        plans.value = response.data.value || [];
        
        // If there are plans, set the first one as active
        if (plans.value.length > 0) {
            plan.value = plans.value[0];
            plan_content.value = JSON.parse(plans.value[0].content);
            plan_reasoning.value = plan_content.value.reasoning;
            plan_analysis_complete.value = plan_content.value.analysis_complete;
        }
    } catch (error) {
        console.error('Error fetching plans:', error);
        throw error;
    }
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

// Initialize reasoningCollapsed based on content availability
watch(() => localCompletion.value?.completion?.content, (newContent) => {
    // When content is available (not empty), collapse the reasoning
    // When content is empty, keep reasoning open
    reasoningCollapsed.value = !!(newContent && newContent.length > 0);
}, { immediate: true });

const plans = ref([]);
const activePlanIndex = ref(0);

const switchPlan = (index) => {
    if (index >= 0 && index < plans.value.length) {
        activePlanIndex.value = index;
        plan.value = plans.value[index];
        plan_content.value = JSON.parse(plans.value[index].content);
        plan_reasoning.value = plan_content.value.reasoning;
        plan_analysis_complete.value = plan_content.value.analysis_complete;
    }
}

// Add new reactive variables for instruction modal
const showInstructionModal = ref(false);

// Add new methods for instruction modal
const openInstructionModal = () => {
    showInstructionModal.value = true;
}

const handleInstructionSaved = (savedInstruction: any) => {
    // Handle the saved instruction - you can add any logic here
    console.log('Instruction saved:', savedInstruction);
    showInstructionModal.value = false;
}

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

@keyframes simple-ellipsis {
    0% { content: '.'; }
    33% { content: '..'; }
    66% { content: '...'; }
}

.simple-dots::after {
    content: '.';
    display: inline-block;
    margin-top: 5px;
    animation: simple-ellipsis 1.5s infinite;
    font-weight: 400;
    font-size: 14px;
    color: #888;
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

/* Add these new transition styles */
.fade-enter-active,
.fade-leave-active {
    transition: opacity 0.3s ease;
}

.fade-enter-from,
.fade-leave-to {
    opacity: 0;
}

.reasoning-content {
    opacity: 0.75;
    transition: opacity 0.2s ease;
}

.reasoning-content:hover {
    opacity: 1;
}
</style>
