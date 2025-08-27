<template>
    <div class="w-full bg-white h-24 p-5 outline-none text-left">
        <MentionComponent
            ref="mentionComponentRef"
            v-model="localTextContent"
            :categories="categories"
            placeholder="Enter text.. 
            Use @ to mention data sources, files, or memories"
            @update:modelValue="handleMentionUpdate"
            @mentionsUpdated="handleMentionsUpdated"
            @submit-content="submitCompletion"
        />
    </div>
          <div class="flex justify-between items-center">
            <div class="flex items-center gap-0">
              <UTooltip text="Upload files">
                <FileUploadComponent :uploaded_files="uploaded_files" @update:uploadedFiles="uploaded_files = $event" />
              </UTooltip>
                <UTooltip text="Instructions">
                    <button @click="openInstructionsModal" class="text-blue-500 hover:bg-gray-50 rounded-md p-2 flex items-center">
                        <UIcon name="i-heroicons-document-text" class="align-middle" /> 
                        <span class="ml-1 text-xs align-middle">Instructions</span>
                    </button>
                </UTooltip>
                <PromptGuidelinesModal ref="guidelinesModalRef" />
                <InstructionsListModalComponent ref="instructionsModalRef" />
                <UTooltip text="Prompt Guidelines">
                    <button @click="openGuidelinesModal" class="text-blue-500 hover:bg-gray-50 rounded-md p-2 flex items-center">
                        <UIcon name="i-heroicons-light-bulb" class="align-middle" /> 
                        <span class="ml-1 text-xs align-middle">Prompt Guidelines</span>
                    </button>
                </UTooltip>
            </div>
            <button @click="submitCompletion" class="text-blue-500 hover:bg-gray-50 rounded-md p-2">
                <UIcon name="i-heroicons-arrow-right" />
            </button>
          </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';
import MentionComponent from '~/components/excel/MentionComponent.vue';
import FileUploadComponent from '~/components/FileUploadComponent.vue';
import InstructionsListModalComponent from '~/components/InstructionsListModalComponent.vue';

const mentionComponentRef = ref<any>(null);
const localTextContent = ref('');
const memories = ref<any[]>([])
const dataSources = ref<any[]>([])
const files = ref<any[]>([])
const router = useRouter()
const mentions = ref<any[]>([])
const uploaded_files = ref<any[]>([])
const guidelinesModalRef = ref<any>(null)
const instructionsModalRef = ref<any>(null)

const props = defineProps({
       textareaContent: {
           type: String,
           default: ''
       }
   });

const categories = ref<Array<{name: string, items: any[]}>>([
    { name: 'DATA SOURCES', items: [] },
    { name: 'MEMORY', items: [] },
    { name: 'FILES', items: [] },
])
const emit = defineEmits(['submitCompletion', 'update:modelValue'])

// Watch for changes in the textareaContent prop
watch(() => props.textareaContent, (newValue) => {
    if (newValue !== localTextContent.value) {
        localTextContent.value = newValue;
        // Force update the MentionComponent's content
        if (mentionComponentRef.value) {
            mentionComponentRef.value.clearContent();
            mentionComponentRef.value.updateContent(newValue);
        }
    }
}, { immediate: true });

function handleMentionUpdate(value: string) {
    localTextContent.value = value;
    emit('update:modelValue', value);
}

function handleMentionsUpdated(mentions: any) {
    console.log('Mentions updated:', mentions)
}

async function getMemories() {
  const response = await useMyFetch(`/api/memories`, {
    method: 'GET',
  });
  if (response.data.value) {
    memories.value = response.data.value as any[];
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
    dataSources.value = response.data.value as any[];

    categories.value = categories.value.map(category => 
      category.name === 'DATA SOURCES' 
        ? { ...category, items: dataSources.value } 
        : category
    );
  }
}
watch(mentions, (newMentions) => {
}, { deep: true });

function submitCompletion() {
  if (localTextContent.value.trim()) {
    createReport();
  }
}


const createReport = async () => {
  const response = await useMyFetch('/reports', {
      method: 'POST',
      body: JSON.stringify({
        title: 'untitled report',
        files: uploaded_files.value?.map((file: any) => file.id) || [],
        new_message: localTextContent.value,
        data_sources: dataSources.value?.map((ds: any) => ds.id) || []
      })
  });

  if (response.error.value) {
      throw new Error('Report creation failed');
  }

  const data = response.data.value as any;
  router.push({
      path: `/reports/${data.id}`,
      query: {
        new_message: localTextContent.value
      }
  })
}


onMounted(async () => {
    nextTick(async () => {
        const { organization, ensureOrganization } = useOrganization()
        
        try {
            // Wait for organization to be available before making API calls
            await ensureOrganization()
            
            if (organization.value?.id) {
                //await getFiles();
                await getMemories();
                await getDataSources();
            } else {
                console.warn('PromptBox: Organization not available, skipping API calls')
            }
        } catch (error) {
            console.error('PromptBox: Error during initialization:', error)
        }
    });
});

// Add this watch effect after the other watch statements
watch(uploaded_files, (newFiles) => {
    categories.value = categories.value.map(category => 
        category.name === 'FILES'
            ? { ...category, items: newFiles }
            : category
    );
}, { deep: true });

const openGuidelinesModal = () => {
    guidelinesModalRef.value?.openModal()
}

const openInstructionsModal = () => {
    instructionsModalRef.value?.openModal()
}
</script>