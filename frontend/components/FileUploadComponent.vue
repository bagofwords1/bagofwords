<template>
    <div class="inline">
      <button @click="isFilesOpen = true"
       class="text-gray-500 hover:text-gray-900 hover:bg-gray-50 rounded-md p-1 flex items-center">
        <UIcon name="i-heroicons-paper-clip" />
        <span v-if="allFiles.length > 0" class="truncate max-w-[200px] text-xs ml-1 text-gray-500">
          <UTooltip :text="allFiles.map(file => file.filename).join(', ')">
          {{ allFiles.length }}
        </UTooltip>
        </span>
      </button>
      <UModal v-model="isFilesOpen">
        <div class="p-4 h-72">
          <h2 class="text-md font-semibold pb-2">Upload files</h2>
          <hr />

          <span class="text-sm text-gray-500 mt-4 mb-2 block">Upload excel or PDF files to analyze</span>
          <input 
            type="file" 
            ref="fileInput" 
            @change="handleFilesUpload" 
            class="hidden" 
            multiple 
          />
          <div 
            v-if="allFiles.length === 0"
            @dragover.prevent="isDragging = true"
            @dragenter.prevent="isDragging = true"
            @dragleave.prevent="isDragging = false"
            @drop.prevent="handleDrop"
            :class="['drop-zone cursor-pointer', isDragging ? 'drop-zone-active' : '']"
            @click="$refs.fileInput.click()"
          > 
            <div class="flex mt-2 flex-col items-center justify-center py-10">
              <Icon 
                name="heroicons-cloud-arrow-up" 
                :class="['w-12 h-12 transition-colors', isDragging ? 'text-blue-500' : 'text-blue-400']" 
              />
              <span class="mt-3 text-sm text-blue-500">
                {{ isDragging ? 'Drop files here' : 'Click or drag files to upload' }}
              </span>
            </div>
          </div>
          <ul
            v-if="allFiles.length > 0"
            class="w-full mt-4"
            @dragover.prevent="isDragging = true"
            @dragenter.prevent="isDragging = true"
            @dragleave.prevent="isDragging = false"
            @drop.prevent="handleDrop">
            <li 
              v-for="(file, index) in allFiles" 
              :key="file.id" 
              class="text-xs py-2 text-gray-600 flex items-center justify-between border-b border-gray-100 last:border-b-0">
              <div class="flex items-center gap-1.5">
                <Spinner v-if="file.status === 'processing'" class="w-3 h-3 text-blue-500 flex-shrink-0" />
                <Icon v-else-if="file.status === 'uploaded'" name="heroicons-check-circle" class="text-blue-500 w-4 h-4 flex-shrink-0" />
                <Icon v-else-if="file.status === 'error'" name="heroicons-x-circle" class="text-red-500 w-4 h-4 flex-shrink-0" />

                <span class="truncate">{{ file.filename }}</span>
              </div>
              <div>
              <button @click="removeFile(file)" class="text-gray-500 hover:bg-gray-100 rounded-full ml-auto items-center justify-center"> 
                <Icon name="heroicons-x-mark" class="w-4 h-4" />
              </button>
            </div>
            </li>
            <li 
              :class="['text-center items-center py-4 mt-3 rounded-lg transition-all cursor-pointer', 
                isDragging ? 'bg-blue-50 border-1 border-dashed border-blue-400' : 'border-2 border-dashed border-gray-200 hover:border-blue-300 hover:bg-blue-50/50']" 
              v-if="allFiles.length > 0"
              @click="$refs.fileInput.click()">
              <div class="text-sm text-blue-500 flex items-center justify-center gap-2 w-full">
                <Icon name="heroicons-cloud-arrow-up" class="w-5 h-5" />
                {{ isDragging ? 'Drop files here' : 'Click or drag to upload more' }}
              </div>
            </li>
          </ul>
        </div>
      </UModal>
    </div>
  </template>
  
<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
import Spinner from './Spinner.vue';

  
const isFilesOpen = ref(false);
const allFiles = ref([]);
const isDragging = ref(false);

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
      // Filter out files that have been used in a completion (completion_id is set)
      // This allows newly uploaded images to show, but hides them after they're submitted
      const unusedFiles = data.value.filter(file => !file.completion_id);
      allFiles.value = unusedFiles.map(file => ({ ...file, status: 'uploaded' }));
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
    // Emit immediately so parent can show processing state
    emit('update:uploadedFiles', [...allFiles.value]);
    selectedFiles.forEach(file => uploadFile(file));
  }

  function handleDrop(e) {
    isDragging.value = false;
    const droppedFiles = Array.from(e.dataTransfer.files).map(file => ({
      id: generateUniqueId(),
      file,
      filename: file.name,
      status: "processing"
    }));
    allFiles.value.push(...droppedFiles);
    // Emit immediately so parent can show processing state
    emit('update:uploadedFiles', [...allFiles.value]);
    droppedFiles.forEach(file => uploadFile(file));
  }
  
  async function uploadFile(file) {
    const formData = new FormData();
    formData.append('file', file.file); // Use the actual File object

    // Add report_id to formData if it exists
    if (report_id) {
      formData.append('report_id', report_id);
    }

    // Update file status to 'processing' before the upload starts
    const index = allFiles.value.findIndex(f => f.id === file.id);
    if (index !== -1) {
      allFiles.value[index] = { ...allFiles.value[index], status: 'processing' };
    }

    try {
      const { data, error } = await useMyFetch('/files', {
        method: 'POST',
        body: formData,
      });

      // Check for errors in the response
      if (error.value || !data.value) {
        console.error('Error uploading file:', error.value);
        const idx = allFiles.value.findIndex(f => f.id === file.id);
        if (idx !== -1) {
          allFiles.value[idx] = { ...allFiles.value[idx], status: 'error' };
        }
        return;
      }

      // Update the file status after successful upload
      const successIdx = allFiles.value.findIndex(f => f.id === file.id);
      if (successIdx !== -1) {
        allFiles.value[successIdx] = { ...data.value, status: 'uploaded' };
      }

      // Emit the updated list of files to the parent component
      emit('update:uploadedFiles', [...allFiles.value]);
    } catch (error) {
      console.error('Error uploading file:', error);
      // Update file status to 'error'
      const idx = allFiles.value.findIndex(f => f.id === file.id);
      if (idx !== -1) {
        allFiles.value[idx] = { ...allFiles.value[idx], status: 'error' };
      }
      // Emit updated list with error status
      emit('update:uploadedFiles', [...allFiles.value]);
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

    // Emit the updated list of files
    emit('update:uploadedFiles', [...allFiles.value]);
  }

  onMounted(async () => {
    await getReportFiles();
    // Emit existing files so parent knows about them
    emit('update:uploadedFiles', [...allFiles.value]);
  });

  // Programmatically upload files (for drag & drop from parent)
  function uploadFilesFromParent(files: FileList | File[]) {
    const fileArray = Array.from(files).map(file => ({
      id: generateUniqueId(),
      file,
      filename: file.name,
      status: "processing" as const
    }));
    allFiles.value.push(...fileArray);
    // Emit immediately so parent can show processing state
    emit('update:uploadedFiles', [...allFiles.value]);
    fileArray.forEach(file => uploadFile(file));
  }

  // Clear image files from local state (no API call - backend handles deletion)
  function clearImages() {
    allFiles.value = allFiles.value.filter(f => {
      const contentType = f.content_type || f.type || ''
      return !contentType.startsWith('image/')
    })
    emit('update:uploadedFiles', [...allFiles.value])
  }

  // Expose methods for parent component
  defineExpose({
    refresh: async () => {
      await getReportFiles();
      emit('update:uploadedFiles', [...allFiles.value]);
    },
    // Upload files programmatically (for drag & drop on prompt area)
    uploadFiles: uploadFilesFromParent,
    // Remove a file (for inline display remove button)
    removeFile,
    // Clear images from local state (called on submit, backend deletes them)
    clearImages,
    // Open the file modal
    open: () => { isFilesOpen.value = true; }
  });

  </script>
  
<style scoped>
.drop-zone {
  border: 1px dashed #e5e7eb;
  border-radius: 12px;
  transition: all 0.2s ease;
  margin-top: 0.75rem;
}

.drop-zone:hover {
  border-color: #93c5fd;
  background-color: #f0f9ff;
}

.drop-zone-active {
  border-color: #3b82f6;
  background-color: #eff6ff;
}
</style>