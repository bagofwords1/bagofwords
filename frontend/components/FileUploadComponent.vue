<template>
    <div class="inline">
      <button @click="isFilesOpen = true"
       class="text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white hover:bg-gray-50 dark:hover:bg-gray-800 rounded-md p-1 flex items-center">
        <UIcon name="i-heroicons-paper-clip" />
        <span v-if="allFiles.length > 0" class="truncate max-w-[200px] text-xs ms-1 text-gray-500 dark:text-gray-400">
          <UTooltip :text="allFiles.map(file => file.filename).join(', ')">
          {{ allFiles.length }}
        </UTooltip>
        </span>
      </button>
      <UModal v-model="isFilesOpen">
        <div class="p-4 min-h-72 flex flex-col">
          <h2 class="text-md font-semibold pb-2">{{ $t('filePicker.title') }}</h2>
          <hr />

          <!-- Search box — filters every section below at once. -->
          <div class="relative mt-3">
            <Icon name="heroicons-magnifying-glass" class="w-4 h-4 text-gray-400 absolute start-2.5 top-1/2 -translate-y-1/2" />
            <input
              v-model="searchQuery"
              type="text"
              :placeholder="$t('filePicker.searchPlaceholder')"
              class="w-full ps-8 pe-3 py-1.5 text-sm rounded-md border border-gray-200 dark:border-gray-700 bg-transparent focus:outline-none focus:border-blue-400"
            />
          </div>

          <input type="file" ref="fileInput" @change="handleFilesUpload" class="hidden" multiple />

          <!-- Selected files (chips in progress) -->
          <ul
            v-if="allFiles.length > 0"
            class="w-full mt-3 max-h-40 overflow-y-auto"
            @dragover.prevent="isDragging = true"
            @dragenter.prevent="isDragging = true"
            @dragleave.prevent="isDragging = false"
            @drop.prevent="handleDrop">
            <li
              v-for="file in allFiles"
              :key="file.id"
              class="text-xs py-2 text-gray-600 dark:text-gray-400 flex items-center justify-between border-b border-gray-100 dark:border-gray-800 last:border-b-0">
              <div class="flex items-center gap-1.5 min-w-0 flex-1">
                <Spinner v-if="file.status === 'processing'" class="w-3 h-3 text-blue-500 flex-shrink-0" />
                <Icon v-else-if="file.status === 'uploaded'" name="heroicons-check-circle" class="text-blue-500 w-4 h-4 flex-shrink-0" />
                <Icon v-else-if="file.status === 'error'" name="heroicons-x-circle" class="text-red-500 w-4 h-4 flex-shrink-0" />
                <span class="truncate">{{ file.filename }}</span>
              </div>
              <div class="flex-shrink-0 ps-2">
                <button @click="removeFile(file)" class="text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-full ms-auto items-center justify-center">
                  <Icon name="heroicons-x-mark" class="w-4 h-4" />
                </button>
              </div>
            </li>
          </ul>

          <!-- Upload from computer -->
          <div class="w-full mt-4">
            <div class="text-2xs font-semibold text-ink-muted uppercase tracking-wider mb-1">
              {{ $t('filePicker.uploadFromComputer') }}
            </div>
            <div
              @dragover.prevent="isDragging = true"
              @dragenter.prevent="isDragging = true"
              @dragleave.prevent="isDragging = false"
              @drop.prevent="handleDrop"
              :class="['drop-zone cursor-pointer', isDragging ? 'drop-zone-active' : '']"
              @click="$refs.fileInput.click()">
              <div class="flex flex-col items-center justify-center py-6">
                <Icon name="heroicons-cloud-arrow-up" :class="['w-8 h-8 transition-colors', isDragging ? 'text-blue-500' : 'text-blue-400']" />
                <span class="mt-2 text-sm text-blue-500">
                  {{ isDragging ? $t('filePicker.dropHere') : $t('filePicker.clickOrDrag') }}
                </span>
              </div>
            </div>
          </div>

          <!-- Recent uploads — only this user's own uploads. -->
          <div v-if="recentUploads.length > 0" class="w-full mt-4">
            <div class="flex items-center justify-between mb-1">
              <div class="text-2xs font-semibold text-ink-muted uppercase tracking-wider">
                {{ $t('filePicker.recentUploads') }}
              </div>
              <button
                v-if="!showAllRecent && recentHasMore"
                @click="showAllRecent = true; loadRecentUploads()"
                class="text-2xs text-blue-500 hover:underline">
                {{ $t('filePicker.browseAll') }}
              </button>
            </div>
            <ul class="w-full max-h-40 overflow-y-auto">
              <li
                v-for="file in recentUploads"
                :key="file.id"
                @click="pickExisting(file)"
                class="text-xs py-1.5 px-1 text-gray-600 dark:text-gray-400 flex items-center gap-1.5 rounded cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800">
                <Icon name="heroicons-document" class="w-3.5 h-3.5 flex-shrink-0 text-ink-muted" />
                <span class="truncate flex-1">{{ file.filename }}</span>
                <Spinner v-if="picking[file.id]" class="w-3 h-3 text-blue-500 flex-shrink-0" />
              </li>
            </ul>
          </div>

          <!-- Files inherited from the report's project (read-only). -->
          <div v-if="projectFiles.length > 0" class="w-full mt-4">
            <div class="text-2xs font-semibold text-ink-muted uppercase tracking-wider mb-1">
              {{ $t('projects.overview.fromProject', { name: projectName }) }}
            </div>
            <ul class="w-full max-h-32 overflow-y-auto">
              <li
                v-for="file in projectFiles"
                :key="file.id"
                class="text-xs py-1.5 text-gray-500 dark:text-gray-400 flex items-center gap-1.5 border-b border-gray-100 dark:border-gray-800 last:border-b-0">
                <Icon name="heroicons-folder" class="w-3.5 h-3.5 flex-shrink-0 text-ink-muted" />
                <span class="truncate">{{ file.filename }}</span>
              </li>
            </ul>
          </div>

          <!-- Google Drive / OneDrive — per-user connections only. -->
          <div
            v-for="conn in visibleConnections"
            :key="conn.connection_id"
            class="w-full mt-4">
            <div class="text-2xs font-semibold text-ink-muted uppercase tracking-wider mb-1 flex items-center gap-1.5">
              <DataSourceIcon v-if="conn.type" :type="conn.type" class="w-3.5 h-3.5" />
              <span class="truncate">{{ conn.name }}</span>
            </div>

            <div v-if="!conn.connected" class="py-2">
              <button
                @click="connectDrive(conn)"
                class="text-xs px-2.5 py-1 rounded-md border border-blue-300 text-blue-600 dark:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-950">
                {{ $t('filePicker.connect') }}
              </button>
            </div>
            <div v-else>
              <div v-if="conn.loading" class="py-2 flex items-center gap-1.5 text-xs text-gray-400">
                <Spinner class="w-3 h-3 text-blue-500" /> {{ $t('filePicker.searching') }}
              </div>
              <ul v-else-if="conn.files.length > 0" class="w-full max-h-40 overflow-y-auto">
                <li
                  v-for="file in conn.files"
                  :key="file.id"
                  @click="pickDrive(conn, file)"
                  class="text-xs py-1.5 px-1 text-gray-600 dark:text-gray-400 flex items-center gap-1.5 rounded cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800">
                  <Icon name="heroicons-document" class="w-3.5 h-3.5 flex-shrink-0 text-ink-muted" />
                  <span class="truncate flex-1">{{ file.name }}</span>
                  <Spinner v-if="picking[conn.connection_id + ':' + file.id]" class="w-3 h-3 text-blue-500 flex-shrink-0" />
                </li>
              </ul>
              <div v-else-if="searchTerm" class="py-2 text-xs text-gray-400">
                {{ $t('filePicker.noResults') }}
              </div>
            </div>
          </div>
        </div>
      </UModal>
    </div>
  </template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, watch } from 'vue';
import Spinner from './Spinner.vue';
import DataSourceIcon from './DataSourceIcon.vue';


const isFilesOpen = ref(false);
const allFiles = ref<any[]>([]);
const isDragging = ref(false);

// Picker state.
const searchQuery = ref('');
const searchTerm = ref('');           // debounced value that actually drives searches
const recentUploads = ref<any[]>([]);
const recentHasMore = ref(false);
const showAllRecent = ref(false);
const fileConnections = ref<any[]>([]);
const picking = reactive<Record<string, boolean>>({});

const RECENT_LIMIT = 7;
const RECENT_ALL_LIMIT = 50;
const DRIVE_RECENT_LIMIT = 5;
const DRIVE_SEARCH_LIMIT = 20;

  const props = defineProps({
    report_id: String,
    // Project (folder) of the report — its files are inherited live into the
    // agent context, so the modal lists them read-only for visibility.
    project: { type: Object, default: null },
  })

  const report_id = props.report_id;

  const projectFiles = ref<any[]>([]);
  const projectName = computed(() => (props.project as any)?.name || '');

  const emit = defineEmits(['update:uploadedFiles']);

  async function loadProjectFiles() {
    if (!(props.project as any)?.id) { projectFiles.value = []; return }
    try {
      const resp = await useMyFetch(`/projects/${(props.project as any).id}`, { method: 'GET' });
      const proj = (resp as any).data?.value;
      projectFiles.value = Array.isArray(proj?.files) ? proj.files : [];
    } catch { projectFiles.value = [] }
  }

  // Drive sections to render: a not-connected one shows its Connect button; a
  // connected one shows only while it is loading, has results, or a search is
  // active (so an empty connected drive with no query stays hidden).
  const visibleConnections = computed(() =>
    fileConnections.value.filter((c) =>
      !c.connected || c.loading || (c.files && c.files.length) || searchTerm.value,
    ),
  );

  async function getReportFiles() {
    if (report_id) {
      const { data } = await useMyFetch(`/reports/${report_id}/files`, { method: 'GET' });
      // Hide files tagged to a completion (already submitted) and files the
      // agent inherited from a data source (those belong to the agent).
      const unusedFiles = (data.value || []).filter(file => !file.completion_id && !file.from_data_source);
      allFiles.value = unusedFiles.map(file => ({ ...file, status: 'uploaded' }));
    }
  }

  function generateUniqueId() {
    return `${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
  }

  // ---- Recent uploads --------------------------------------------------------

  async function loadRecentUploads() {
    const limit = showAllRecent.value ? RECENT_ALL_LIMIT : RECENT_LIMIT;
    const params = new URLSearchParams({ mine: 'true', source_kind: 'upload', limit: String(limit) });
    if (searchTerm.value) params.set('q', searchTerm.value);
    try {
      const { data } = await useMyFetch(`/files?${params.toString()}`, { method: 'GET' });
      const rows = Array.isArray(data.value) ? data.value : [];
      const chosenIds = new Set(allFiles.value.map((f) => f.id));
      // Don't offer a file that's already a chip.
      recentUploads.value = rows.filter((f) => !chosenIds.has(f.id));
      recentHasMore.value = !showAllRecent.value && rows.length >= RECENT_LIMIT;
    } catch {
      recentUploads.value = [];
    }
  }

  async function pickExisting(file: any) {
    if (picking[file.id]) return;
    picking[file.id] = true;
    try {
      const body: any = { file_id: file.id };
      if (report_id) body.report_id = report_id;
      const { data, error } = await useMyFetch('/files/attach', { method: 'POST', body });
      if (error.value || !data.value) return;
      addChip(data.value);
      recentUploads.value = recentUploads.value.filter((f) => f.id !== file.id);
    } finally {
      delete picking[file.id];
    }
  }

  // ---- Drive / OneDrive connections -----------------------------------------

  async function loadConnections() {
    try {
      const { data } = await useMyFetch('/connections', { method: 'GET' });
      const rows = Array.isArray(data.value) ? data.value : [];
      fileConnections.value = rows
        // Only per-user file connections — never a shared org login, and never
        // a non-file source.
        .filter((c: any) => c.data_shape === 'files' && c.auth_policy === 'user_required'
          && Array.isArray(c.data_source_ids) && c.data_source_ids.length > 0)
        .map((c: any) => ({
          connection_id: c.id,
          data_source_id: c.data_source_ids[0],
          name: c.name,
          type: c.type,
          connected: !!(c.user_status && c.user_status.has_user_credentials),
          loading: false,
          files: [] as any[],
        }));
      await Promise.all(fileConnections.value.map((c) => loadDriveFiles(c)));
    } catch {
      fileConnections.value = [];
    }
  }

  async function loadDriveFiles(conn: any) {
    if (!conn.connected) return;
    conn.loading = true;
    // One generation per keystroke (bumped in the search watcher), captured
    // before the request — so a slow drive whose generation is superseded
    // drops its result, but parallel drives in the SAME generation don't
    // cancel each other (a shared per-call counter did exactly that).
    const gen = searchGeneration;
    try {
      let url: string;
      if (searchTerm.value) {
        const p = new URLSearchParams({ q: searchTerm.value, limit: String(DRIVE_SEARCH_LIMIT) });
        url = `/data_sources/${conn.data_source_id}/connections/${conn.connection_id}/files/search?${p.toString()}`;
      } else {
        url = `/data_sources/${conn.data_source_id}/connections/${conn.connection_id}/files?limit=${DRIVE_RECENT_LIMIT}`;
      }
      const { data } = await useMyFetch(url, { method: 'GET' });
      // A stale response (the user kept typing) must not overwrite newer results.
      if (gen !== searchGeneration) return;
      const body: any = data.value || {};
      if (body.connect_required) { conn.connected = false; conn.files = []; return; }
      conn.files = (Array.isArray(body.files) ? body.files : []).slice(0, searchTerm.value ? DRIVE_SEARCH_LIMIT : DRIVE_RECENT_LIMIT);
    } catch {
      conn.files = [];
    } finally {
      conn.loading = false;
    }
  }

  async function pickDrive(conn: any, file: any) {
    const key = conn.connection_id + ':' + file.id;
    if (picking[key]) return;
    picking[key] = true;
    try {
      const body: any = {
        data_source_id: conn.data_source_id,
        connection_id: conn.connection_id,
        ref: file.id,
      };
      if (report_id) body.report_id = report_id;
      const { data, error } = await useMyFetch('/files/attach', { method: 'POST', body });
      if (error.value || !data.value) return;
      addChip(data.value);
    } finally {
      delete picking[key];
    }
  }

  async function connectDrive(conn: any) {
    try {
      const returnTo = typeof window !== 'undefined' ? window.location.pathname : '/';
      const { data } = await useMyFetch(
        `/connections/${conn.connection_id}/oauth/authorize?return_to=${encodeURIComponent(returnTo)}`,
        { method: 'GET' },
      );
      const url = (data.value as any)?.authorization_url;
      if (url && typeof window !== 'undefined') window.location.href = url;
    } catch { /* leave the Connect button in place on failure */ }
  }

  // A picked file (existing or copied) joins the chip list as an uploaded file.
  function addChip(file: any) {
    if (allFiles.value.some((f) => f.id === file.id)) return;
    allFiles.value.push({ ...file, status: 'uploaded' });
    emit('update:uploadedFiles', [...allFiles.value]);
  }

  // ---- Debounced search ------------------------------------------------------

  let searchGeneration = 0;  // bumped once per keystroke so a slow drive can't clobber newer results
  let debounceTimer: any = null;
  watch(searchQuery, (q) => {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      searchTerm.value = (q || '').trim();
      showAllRecent.value = false;
      searchGeneration++;
      loadRecentUploads();
      // Each drive searches independently and in parallel — a slow one never
      // holds up the others.
      fileConnections.value.forEach((c) => loadDriveFiles(c));
    }, 300);
  });

  watch(isFilesOpen, async (open) => {
    if (!open) return;
    await Promise.all([getReportFiles(), loadRecentUploads(), loadConnections(), loadProjectFiles()]);
    emit('update:uploadedFiles', [...allFiles.value]);
  });

  // ---- Upload from computer (unchanged behavior) -----------------------------

  function handleFilesUpload(e) {
    const selectedFiles = Array.from(e.target.files).map(file => ({
      id: generateUniqueId(), file, filename: file.name, status: "processing"
    }));
    allFiles.value.push(...selectedFiles);
    emit('update:uploadedFiles', [...allFiles.value]);
    selectedFiles.forEach(file => uploadFile(file));
  }

  function handleDrop(e) {
    isDragging.value = false;
    const droppedFiles = Array.from(e.dataTransfer.files).map(file => ({
      id: generateUniqueId(), file, filename: file.name, status: "processing"
    }));
    allFiles.value.push(...droppedFiles);
    emit('update:uploadedFiles', [...allFiles.value]);
    droppedFiles.forEach(file => uploadFile(file));
  }

  async function uploadFile(file) {
    const formData = new FormData();
    formData.append('file', file.file);
    if (report_id) formData.append('report_id', report_id);

    const index = allFiles.value.findIndex(f => f.id === file.id);
    if (index !== -1) {
      allFiles.value[index] = { ...allFiles.value[index], status: 'processing' };
    }

    try {
      const { data, error } = await useMyFetch('/files', { method: 'POST', body: formData });
      if (error.value || !data.value) {
        const idx = allFiles.value.findIndex(f => f.id === file.id);
        if (idx !== -1) allFiles.value[idx] = { ...allFiles.value[idx], status: 'error' };
        return;
      }
      const successIdx = allFiles.value.findIndex(f => f.id === file.id);
      if (successIdx !== -1) allFiles.value[successIdx] = { ...data.value, status: 'uploaded' };
      emit('update:uploadedFiles', [...allFiles.value]);
    } catch (error) {
      const idx = allFiles.value.findIndex(f => f.id === file.id);
      if (idx !== -1) allFiles.value[idx] = { ...allFiles.value[idx], status: 'error' };
      emit('update:uploadedFiles', [...allFiles.value]);
    }
  }

  async function removeFile(file) {
    allFiles.value = allFiles.value.filter(f => f !== file);
    if (file.id && report_id) {
      try {
        await useMyFetch(`/reports/${report_id}/files/${file.id}`, { method: 'DELETE' });
      } catch (error) {
        console.error('Error deleting file from server:', error);
      }
    }
    emit('update:uploadedFiles', [...allFiles.value]);
  }

  onMounted(async () => {
    await getReportFiles();
    emit('update:uploadedFiles', [...allFiles.value]);
  });

  function uploadFilesFromParent(files: FileList | File[]) {
    const fileArray = Array.from(files).map(file => ({
      id: generateUniqueId(), file, filename: file.name, status: "processing" as const
    }));
    allFiles.value.push(...fileArray);
    emit('update:uploadedFiles', [...allFiles.value]);
    fileArray.forEach(file => uploadFile(file));
  }

  function clearImages() {
    allFiles.value = allFiles.value.filter(f => {
      const contentType = f.content_type || f.type || ''
      return !contentType.startsWith('image/')
    })
    emit('update:uploadedFiles', [...allFiles.value])
  }

  defineExpose({
    refresh: async () => {
      await getReportFiles();
      emit('update:uploadedFiles', [...allFiles.value]);
    },
    uploadFiles: uploadFilesFromParent,
    removeFile,
    clearImages,
    open: () => { isFilesOpen.value = true; }
  });

  </script>

<style scoped>
.drop-zone {
  border: 1px dashed #e5e7eb;
  border-radius: 12px;
  transition: all 0.2s ease;
  margin-top: 0.25rem;
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
