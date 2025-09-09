<template>
    <div class="flex-shrink-0 p-2 bg-white">
      <div 
      v-if="props.selectedWidgetId?.widgetTitle"
      class="text-xs bg-cyan-50 text-cyan-500 rounded-t-xl px-3 border border-cyan-200 py-2 pb-3.5 -mb-2 w-fit">
      <span>
        <Icon name="heroicons-arrow-turn-down-right" />
      </span>
        Update @{{ props.selectedWidgetId?.widgetTitle }}
        <span class="cursor-pointer ml-1" @click.stop="clearSelection">
          &times;
        </span>
      </div>
        <div class="flex flex-col border bg-[#fafafa] border-gray-200 rounded-xl p-2 mb-4 shadow-sm transition-shadow duration-200 hover:shadow-sm hover:border-gray-300">
            <div class="flex flex-col">
                <div class="flex items-center space-x-0.2 mb-2">

                    <DataSourceSelectorComponentExcel v-model:selectedDataSources="selectedDataSources" :report_id="report_id" />
                    <FileUploadExcelComponent v-model:uploadedFiles="uploaded_files" :report_id="report_id" />
                    <div>
                      <button
                                    v-if="canViewInstructions" 
                                    class="text-gray-500 hover:text-gray-400 hover:bg-gray-100 border border-gray-200 text-[11px] px-2 py-1 rounded-md"
                                    @click="openInstructionsListModal"
                                >
                                    <Icon name="heroicons-document-text" /> Instructions
                                </button>
                    </div>
                    <div>
                <button
                    class="text-gray-500 hover:text-gray-400 hover:bg-gray-100 border border-gray-200 ml-2 text-[11px] px-2 py-1 rounded-md"
                    @click="openPromptGuidelines"
                >
                    <Icon name="heroicons-light-bulb" /> Prompt Guidelines
                </button>
                    </div>
                    <div class="flex ml-2 text-[11px] text-gray-500 border border-gray-200 rounded px-1.5 py-1 mt-0.5"
                    v-if="excelData.address && excelData.address.includes(':')">
                        ={{ excelData.address || 'No cell selected' }}
                    </div>
                    <div class="hidden flex ml-2 text-[11px] text-gray-500 border border-gray-200 hover:bg-gray-50 rounded px-1.5 py-1 mt-0.5 cursor-pointer"
                    v-if="props.selectedWidgetId?.widgetId && props.selectedWidgetId?.stepId && props.selectedWidgetId?.widgetTitle"
                    @click="clearSelection"
                    >
                        @{{ props.selectedWidgetId?.widgetTitle }}
                        <span
                        class="cursor-pointer ml-1"
                        @click.stop="clearSelection">
                            &times;
                        </span>

                    </div>

                </div>


            </div>

            <div class="flex space-x-2 items-center">
                <MentionComponent
                    ref="mentionComponentRef"
                    v-model="textContent"
                    :categories="categories"
                    @update:modelValue="handleMentionUpdate"
                    @mentionsUpdated="handleMentionsUpdated"
                    @submit-content="submitCompletion"
                />
                <div class="flex items-center space-x-2">
                    <button
                        v-if="latestInProgressCompletion"
                        class="text-gray-600 hover:text-gray-800  px-2 py-2 rounded-md"
                        :disabled="isStopping"
                        @click="$emit('stopGeneration')"
                    >
                    
                  <UTooltip text="Stop Generation">
                        <Icon name="heroicons-stop-solid" class="w-4 h-4 inline-block text-black" />
                      </UTooltip>
                    </button>

                    <button
                    v-else
                        class="text-blue-500 hover:text-blue-400 hover:bg-gray-100 px-2 py-2 rounded-md"
                        :disabled="isStopping || latestInProgressCompletion"
                        @click="submitCompletion"
                    >
                        <Icon name="heroicons-arrow-right" />
                    </button>
                </div>

            </div>
            <div class="text-[10px] mt-0.5 text-gray-500">
                @ Mention to add things from memory
            </div>
            <PromptGuidelinesModal ref="promptGuidelinesModalRef" />
            <InstructionsListModalComponent ref="instructionsListModalRef" />
            <InstructionModalComponent
        v-model="showInstructionModal"
        :instruction="null"
        @instructionSaved="handleInstructionSaved"
    />
        </div>
    </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue';
import DataSourceSelectorComponentExcel from './DataSourceSelectorComponentExcel.vue';
import FileUploadExcelComponent from './FileUploadExcelComponent.vue';
import MentionComponent from './MentionComponent.vue';
import PromptGuidelinesModal from '../PromptGuidelinesModal.vue';
import InstructionsListModalComponent from '../InstructionsListModalComponent.vue';
import { usePermissionsLoaded } from '~/composables/usePermissions'

const props = defineProps({
    promptsSuggestions: Array,
    columnsSuggestions: Array,
    widgets: Array,
    report_id: String,
    excelData: Object,
    selectedWidgetId: Object,
    latestInProgressCompletion: Object,
    isStopping: Boolean,
});

const report_id = props.report_id

const uploaded_files = ref([])

const selectedDataSources = ref([])

watch(() => props.selectedWidgetId, (newValue) => {
}, { deep: true, immediate: true });

const excelData = ref(props.excelData);

// Add this watch to update excelData when props.excelData changes
watch(() => props.excelData, (newValue) => {
    excelData.value = newValue;
}, { deep: true });

const emit = defineEmits(['submitCompletion','stopGeneration']);
const inputRef = ref<HTMLDivElement | null>(null);
const textContent = ref('');
const mentions = ref([
    {
        name: 'MEMORY',
        items: []
    },
    {
        name: 'FILES',
        items: []
    },
    {
        name: 'DATA SOURCES',
        items: []
    },
]);

const showDropdown = ref(false);
const selectedIndex = ref(-1);
const currentMentionStartIndex = ref(-1);

const memories = ref([]);
const files = ref([]);
const dataSources = ref([]);

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

async function getFiles() {
  const response = await useMyFetch(`/api/files`, {
    method: 'GET',
  });
  if (response.data.value) {
    files.value = response.data.value;

    categories.value = categories.value.map(category => 
      category.name === 'FILES' 
        ? { ...category, items: files.value } 
        : category
    );
  }
}

function getWidgetTitle(widgetId: string) {
    //console.log('Getting title for widget:', widgetId, 'Available widgets:', props.widgets); // Debug log
    const widget = props.widgets?.find(w => w.id === widgetId);
    if (!widget) {
        console.log('Widget not found:', widgetId); // Debug log
    }
    return widget?.title || null;
}

async function getMemories() {
  const response = await useMyFetch(`/api/memories`, {
    method: 'GET',
  });
  if (response.data.value) {
    memories.value = response.data.value;
    // Update the MEMORY category with the fetched memories
    categories.value = categories.value.map(category => 
      category.name === 'MEMORY' 
        ? { ...category, items: memories.value } 
        : category
    );
  }
}

async function getDataSources() {
  const response = await useMyFetch(`/api/data_sources/active`, {
    method: 'GET',
  });
  if (response.data.value) {
    dataSources.value = response.data.value;

    categories.value = categories.value.map(category => 
      category.name === 'DATA SOURCES' 
        ? { ...category, items: dataSources.value } 
        : category
    );
  }
}

const categories = ref([
    {
        name: 'MEMORY',
        items: []
    },
    {
        name: 'FILES',
        items: []
    },
        
    {
        name: 'DATA SOURCES',
        items: []
    },
]);

function handleMentionUpdate(value: string) {
  textContent.value = value;
}

function handleMentionsUpdated(newMentions: any) {
  mentions.value = JSON.parse(JSON.stringify(newMentions));
}

watch(mentions, (newMentions) => {
}, { deep: true });

function submitCompletion() {
  emit('submitCompletion', { text: textContent.value, mentions: mentions.value });
  textContent.value = '';
  if (mentionComponentRef.value) {
    mentionComponentRef.value.updateContent('');
  }
  clearSelection();
}

onMounted(async () => {
    nextTick(async () => {
        const { organization, ensureOrganization } = useOrganization()
        
        try {
            // Wait for organization to be available before making API calls
            await ensureOrganization()
            
            if (organization.value?.id) {
                await getFiles();
                await getMemories();
                await getDataSources();
            } else {
                console.warn('PromptBoxExcel: Organization not available, skipping API calls')
            }
        } catch (error) {
            console.error('PromptBoxExcel: Error during initialization:', error)
        }
    });
});

onMounted(() => {
});

function clearSelection() {
    emit('update:selectedWidgetId', null, null);
}

// Add a watch for the widgets prop
watch(() => props.widgets, (newWidgets) => {
}, { deep: true, immediate: true });

const mentionComponentRef = ref(null);

// Add method to update prompt content
function updatePromptContent(content: string) {
    if (mentionComponentRef.value) {
        mentionComponentRef.value.updateContent(content);
    }
}

// Expose the method to parent components
defineExpose({
    updatePromptContent
});

const promptGuidelinesModalRef = ref(null);
const instructionsListModalRef = ref(null);

function openPromptGuidelines() {
    promptGuidelinesModalRef.value?.openModal();
}

function openInstructionsListModal() {
    instructionsListModalRef.value?.openModal();
}

// Computed property that waits for permissions to load
const canViewInstructions = computed(() => {
  const permissionsLoaded = usePermissionsLoaded()
  
  // Only check permissions after they're loaded
  if (!permissionsLoaded.value) {
    return false
  }
  
  return useCan('create_instructions') || useCan('view_instructions')
})
</script>

<style scoped>
.mention {
  color: blue;
  background-color: #eee;
  padding: 1px 4px;
  border-radius: 3px;
  font-weight: 500;
  white-space: nowrap;
  user-select: all;
}

[contenteditable]:empty:before {
  content: attr(placeholder);
  color: #888;
  font-style: italic;
}
</style>
