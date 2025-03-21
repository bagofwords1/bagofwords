<template>
    <div class="inline">
      <button @click="isFilesOpen = true"
        class="h-[25px] border border-gray-200 rounded hover:bg-gray-50 text-gray-500 text-[11px] px-1.5 py-0 rounded dark:bg-gray-700 dark:text-gray-300">
        <span v-if="allFiles.length == 0">
        + Add Files
      </span>
      <span v-else class="truncate max-w-[200px] block">
        Files:
        {{ allFiles.map(file => file.filename).join(', ') }}
      </span>
      </button>
      <UModal v-model="isFilesOpen">
        <div class="p-4 h-72">
          <h2 class="text-md font-semibold pb-2">Upload files</h2>
          <hr />

          <span class="text-xs text-gray-500 mt-2 block">Upload excel files to analyze</span>
          <input 
            type="file" 
            ref="fileInput" 
            @change="handleFilesUpload" 
            class="hidden" 
            multiple 
          />
          <div v-if="allFiles.length === 0"> 
          <button @click="$refs.fileInput.click()" class="text-xs text-center w-full pt-5 pb-5 text-blue-500">
                <Icon name="heroicons-folder-plus" class="w-20 h-20 block bg-green-400 mt-4 mx-auto" />
                <span class="mt-4 block">
                   Click to upload
              </span>
              </button>
          </div>
          <ul
            v-if="allFiles.length > 0"
            class="w-full mt-4">
            <li 
              v-for="(file, index) in allFiles" 
              :key="file.id" 
              :class="['text-sm border-t py-1 text-gray-500 mt-1 flex items-center justify-between', 
                       index === allFiles.length - 1 ? 'border-b' : '']">
              <div>
                {{ file.filename }}
                <Icon v-if="file.status === 'processing'" name="heroicons-arrow-path-rounded-square" class="animate-spin inline-block" />
                <Icon v-else-if="file.status === 'uploaded'" name="heroicons-check" class="text-green-500 inline-block" />
                <Icon v-else-if="file.status === 'error'" name="heroicons-x-circle" class="text-red-500 inline-block" />
              </div>
              <div>
              <button @click="removeFile(file)" class="text-gray-500 hover:bg-gray-100 rounded-full ml-auto items-center justify-center"> 
                <Icon name="heroicons-x-mark" class="w-4 h-4" />
              </button>
            </div>
            </li>
            <li class="bg-blue-50 text-center items-center py-1 mt-2" v-if="allFiles.length > 0">
              <button @click="$refs.fileInput.click()" class="text-xs hover:underline text-center text-blue-500">
                Click to upload more
              </button>
            </li>
          </ul>
        </div>
      </UModal>
    </div>
  </template>
  
  <script setup lang="ts">
  import { ref, computed, onMounted } from 'vue';

  
  const isFilesOpen = ref(false);
  const allFiles = ref([]);

  const props = defineProps({
    report_id: String
  })

  const report_id = props.report_id;

  const emit = defineEmits(['update:uploadedFiles']);

  async function getReportFiles() {
    if (report_id) {
      const { data } = await useMyFetch(`/reports/${report_id}/files`, {
        method: 'GET',
      });
      allFiles.value = data.value.map(file => ({ ...file, status: 'uploaded' }));
    }
  }

  function generateUniqueId() {
    return `${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
  }

  function handleFilesUpload(e) {
    const selectedFiles = Array.from(e.target.files).map(file => ({
      id: generateUniqueId(),
      file,
      filename: file.name,
      status: "processing"
    }));
    allFiles.value.push(...selectedFiles);
    selectedFiles.forEach(file => uploadFile(file));
  }
  
  async function uploadFile(file) {
    const formData = new FormData();
    formData.append('file', file.file); // Use the actual File object

    // Add report_id to formData if it exists
    if (report_id) {
      formData.append('report_id', report_id);
    }

    try {
      // Update file status to 'processing' before the upload starts
      const index = allFiles.value.findIndex(f => f.id === file.id);
      if (index !== -1) {
        allFiles.value[index] = { ...allFiles.value[index], status: 'processing' };
      }

      const { data } = await useMyFetch('/files', {
        method: 'POST',
        body: formData,
      });

      // Update the file status after successful upload
      if (index !== -1) {
        allFiles.value[index] = { ...data.value, status: 'uploaded' };
      }

      // Emit the updated list of uploaded files to the parent component
      emit('update:uploadedFiles', allFiles.value.filter(f => f.status === 'uploaded'));
    } catch (error) {
      console.error('Error uploading file:', error);
      // Update file status to 'error'
      const index = allFiles.value.findIndex(f => f.id === file.id);
      if (index !== -1) {
        allFiles.value[index] = { ...allFiles.value[index], status: 'error' };
      }
    }
  }

  async function removeFile(file) {
    // Remove the file from allFiles array
    allFiles.value = allFiles.value.filter(f => f !== file);

    // If the file has an ID and report_id exists, delete it from the server
    if (file.id && report_id) {
      try {
        await useMyFetch(`/reports/${report_id}/files/${file.id}`, {
          method: 'DELETE',
        });
      } catch (error) {
        console.error('Error deleting file from server:', error);
        // Optionally, you can handle the error (e.g., show a notification to the user)
      }
    }

    // Emit the updated list of uploaded files
    emit('update:uploadedFiles', allFiles.value.filter(f => f.status === 'uploaded'));
  }

  onMounted(async () => {
    nextTick(async () => {
      await getReportFiles();
    });
  });
  
  </script>
  
  <style scoped>
  /* Add any specific styles here */
  </style>