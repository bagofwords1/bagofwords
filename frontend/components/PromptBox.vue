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
                <PromptGuidelinesModal ref="guidelinesModalRef" />
                <UTooltip text="Prompt Guidelines">
                    <button @click="openGuidelinesModal" class="text-blue-500 hover:bg-gray-50 rounded-md p-2">
                        <UIcon name="i-heroicons-light-bulb" />
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

const mentionComponentRef = ref(null);
const localTextContent = ref('');
const memories = ref([])
const dataSources = ref([])
const files = ref([])
const router = useRouter()
const mentions = ref([])
const uploaded_files = ref([])
const guidelinesModalRef = ref(null)

const props = defineProps({
       textareaContent: {
           type: String,
           default: ''
       }
   });

const categories = ref([
    { name: 'DATA SOURCES', items: [] },
    { name: 'MEMORY', items: [] },
    { name: 'FILES', items: [] },
])
const emit = defineEmits(['submitCompletion'])

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
        files: uploaded_files.value.map((file: any) => file.id),
        new_message: localTextContent.value,
        data_sources: dataSources.value.map((ds: any) => ds.id)
      })
  });

  if (!response.code === 200) {
      throw new Error('Report creation failed');
  }

  const data = await response.data.value;
  router.push({
      path: `/reports/${data.id}`,
      query: {
        new_message: localTextContent.value
      }
  })
}


onMounted(async () => {
    nextTick(async () => {
        //await getFiles();
        await getMemories();
        await getDataSources();
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
</script>