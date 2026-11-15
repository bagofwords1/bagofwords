<template>
  <div class="inline">
    <input type="file" ref="fileInput" @change="handleFilesUpload" class="hidden" multiple />

    <!-- "Upload file" opens the familiar drag/drop modal (not the OS picker). -->
    <UModal v-model="isUploadModalOpen">
      <div class="p-4 min-h-60 flex flex-col">
        <h2 class="text-md font-semibold pb-2">{{ $t('filePicker.uploadTitle') }}</h2>
        <hr />
        <div
          @dragover.prevent="isDragging = true"
          @dragenter.prevent="isDragging = true"
          @dragleave.prevent="isDragging = false"
          @drop.prevent="handleDrop"
          :class="['drop-zone cursor-pointer mt-4', isDragging ? 'drop-zone-active' : '']"
          @click="fileInput?.click()">
          <div class="flex flex-col items-center justify-center py-10">
            <Icon name="heroicons-cloud-arrow-up" :class="['w-12 h-12 transition-colors', isDragging ? 'text-blue-500' : 'text-blue-400']" />
            <span class="mt-3 text-sm text-blue-500">
              {{ isDragging ? $t('filePicker.dropHere') : $t('filePicker.clickOrDrag') }}
            </span>
          </div>
        </div>
        <ul v-if="allFiles.length > 0" class="w-full mt-4 max-h-56 overflow-y-auto">
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
            <button @click="removeFile(file)" class="flex-shrink-0 ps-2 text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-full">
              <Icon name="heroicons-x-mark" class="w-4 h-4" />
            </button>
          </li>
        </ul>
      </div>
    </UModal>

    <UPopover v-model:open="isOpen" :popper="{ placement: 'bottom-start', offset: [0, 8] }">
      <button
        class="text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white hover:bg-gray-50 dark:hover:bg-gray-800 rounded-md p-1 flex items-center">
        <UIcon name="i-heroicons-paper-clip" />
        <span v-if="allFiles.length > 0" class="truncate max-w-[200px] text-xs ms-1 text-gray-500 dark:text-gray-400">
          <UTooltip :text="allFiles.map(file => file.filename).join(', ')">{{ allFiles.length }}</UTooltip>
        </span>
      </button>

      <template #panel>
        <div class="w-72 p-1.5 text-sm">
          <!-- Search over every section at once -->
          <div class="relative mb-1">
            <Icon name="heroicons-magnifying-glass" class="w-4 h-4 text-gray-400 absolute start-2.5 top-1/2 -translate-y-1/2" />
            <input
              v-model="searchQuery"
              type="text"
              :placeholder="$t('filePicker.searchPlaceholder')"
              class="w-full ps-8 pe-3 py-1.5 text-sm rounded-md border border-gray-200 dark:border-gray-700 bg-transparent focus:outline-none focus:border-blue-400" />
          </div>

          <!-- "Files" drill-in: the user's own uploads -->
          <div v-if="view === 'files'">
            <button @click="goRoot" class="row font-medium text-ink-muted">
              <Icon name="heroicons-chevron-left" class="w-4 h-4 rtl-flip flex-shrink-0" />
              <span class="truncate">{{ $t('filePicker.files') }}</span>
            </button>
            <div class="max-h-60 overflow-y-auto">
              <button v-for="f in recentUploads" :key="f.id" @click="pickExisting(f)" class="row">
                <Icon name="heroicons-document" class="row-ic" />
                <span class="truncate flex-1 text-start">{{ f.filename }}</span>
                <Spinner v-if="picking[f.id]" class="w-3 h-3 text-blue-500 flex-shrink-0" />
              </button>
              <div v-if="recentUploads.length === 0" class="px-2 py-3 text-xs text-gray-400">
                {{ searchTerm ? $t('filePicker.noResults') : $t('filePicker.noFilesYet') }}
              </div>
            </div>
          </div>

          <!-- Drive drill-in: a per-user Google Drive / OneDrive / SharePoint -->
          <div v-else-if="view === 'drive' && activeDrive">
            <button @click="goRoot" class="row font-medium text-ink-muted">
              <Icon name="heroicons-chevron-left" class="w-4 h-4 rtl-flip flex-shrink-0" />
              <DataSourceIcon :type="activeDrive.type" class="row-ic" />
              <span class="truncate">{{ activeDrive.name }}</span>
            </button>
            <div v-if="!activeDrive.connected" class="px-2 py-3">
              <button @click="connectDrive(activeDrive)"
                class="text-xs px-2.5 py-1 rounded-md border border-blue-300 text-blue-600 dark:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-950">
                {{ $t('filePicker.connect') }}
              </button>
            </div>
            <div v-else>
              <div v-if="activeDrive.loading" class="px-2 py-3 flex items-center gap-1.5 text-xs text-gray-400">
                <Spinner class="w-3 h-3 text-blue-500" /> {{ $t('filePicker.searching') }}
              </div>
              <div v-else class="max-h-60 overflow-y-auto">
                <button v-for="f in activeDrive.files" :key="f.id" @click="pickDrive(activeDrive, f)" class="row">
                  <Icon name="heroicons-document" class="row-ic" />
                  <span class="truncate flex-1 text-start">{{ f.name }}</span>
                  <Spinner v-if="picking[activeDrive.connection_id + ':' + f.id]" class="w-3 h-3 text-blue-500 flex-shrink-0" />
                </button>
                <div v-if="activeDrive.files.length === 0" class="px-2 py-3 text-xs text-gray-400">
                  {{ $t('filePicker.noResults') }}
                </div>
              </div>
            </div>
          </div>

          <!-- Flat search results across every source -->
          <div v-else-if="searchTerm">
            <div v-if="flatResults.length === 0" class="px-2 py-3 text-xs text-gray-400">{{ $t('filePicker.noResults') }}</div>
            <div class="max-h-72 overflow-y-auto">
              <button v-for="r in flatResults" :key="r.key" @click="pickResult(r)" class="row">
                <DataSourceIcon v-if="r.kind === 'drive'" :type="r.conn.type" class="row-ic" />
                <Icon v-else name="heroicons-document" class="row-ic" />
                <span class="truncate flex-1 text-start">{{ r.name }}</span>
                <Spinner v-if="r.loading" class="w-3 h-3 text-blue-500 flex-shrink-0" />
              </button>
            </div>
          </div>

          <!-- Root menu -->
          <div v-else>
            <button @click="triggerUpload" class="row">
              <Icon name="heroicons-arrow-up-tray" class="row-ic" />
              <span class="flex-1 text-start">{{ $t('filePicker.uploadFile') }}</span>
            </button>
            <button @click="openFiles" class="row">
              <Icon name="heroicons-document-duplicate" class="row-ic" />
              <span class="flex-1 text-start">{{ $t('filePicker.files') }}</span>
              <Icon name="heroicons-chevron-right" class="w-4 h-4 text-ink-muted rtl-flip flex-shrink-0" />
            </button>
            <button v-for="conn in fileConnections" :key="conn.connection_id" @click="openDrive(conn)" class="row">
              <DataSourceIcon :type="conn.type" class="row-ic" />
              <span class="flex-1 text-start truncate">{{ conn.name }}</span>
              <Icon name="heroicons-chevron-right" class="w-4 h-4 text-ink-muted rtl-flip flex-shrink-0" />
            </button>

            <div v-if="projectFiles.length > 0" class="mt-1 pt-1 border-t border-gray-100 dark:border-gray-800">
              <div class="px-2 py-1 text-2xs font-semibold text-ink-muted uppercase tracking-wider">
                {{ $t('projects.overview.fromProject', { name: projectName }) }}
              </div>
              <div v-for="f in projectFiles" :key="f.id" class="row opacity-70 cursor-default">
                <Icon name="heroicons-folder" class="row-ic" />
                <span class="truncate">{{ f.filename }}</span>
              </div>
            </div>
          </div>
        </div>
      </template>
    </UPopover>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted, watch } from 'vue';
import Spinner from './Spinner.vue';
import DataSourceIcon from './DataSourceIcon.vue';

const isOpen = ref(false);
const allFiles = ref<any[]>([]);

// Menu navigation: 'root' | 'files' | 'drive'
const view = ref<'root' | 'files' | 'drive'>('root');
const activeDrive = ref<any | null>(null);

// Picker data
const searchQuery = ref('');
const searchTerm = ref('');
const recentUploads = ref<any[]>([]);
const fileConnections = ref<any[]>([]);
const picking = reactive<Record<string, boolean>>({});

const RECENT_LIMIT = 20;
const DRIVE_RECENT_LIMIT = 8;
const DRIVE_SEARCH_LIMIT = 20;

const props = defineProps({
  report_id: String,
  // Project (folder) of the report — its files are inherited live into the
  // agent context, so the menu lists them read-only for visibility.
  project: { type: Object, default: null },
});
const report_id = props.report_id;
const projectFiles = ref<any[]>([]);
const projectName = computed(() => (props.project as any)?.name || '');

const emit = defineEmits(['update:uploadedFiles']);

// Flattened search hits across the user's uploads and every connected drive.
const flatResults = computed(() => {
  const out: any[] = [];
  for (const f of recentUploads.value) {
    out.push({ key: 'u:' + f.id, kind: 'file', name: f.filename, file: f, loading: picking[f.id] });
  }
  for (const c of fileConnections.value) {
    if (!c.connected) continue;
    for (const f of (c.files || [])) {
      out.push({ key: c.connection_id + ':' + f.id, kind: 'drive', name: f.name, conn: c, file: f, loading: picking[c.connection_id + ':' + f.id] });
    }
  }
  return out;
});

function goRoot() { view.value = 'root'; activeDrive.value = null; searchQuery.value = ''; }
function openFiles() { searchQuery.value = ''; view.value = 'files'; }
function openDrive(conn: any) {
  searchQuery.value = '';
  activeDrive.value = conn;
  view.value = 'drive';
  if (conn.connected && (!conn.files || conn.files.length === 0)) loadDriveFiles(conn);
}
const fileInput = ref<HTMLInputElement | null>(null);
const isUploadModalOpen = ref(false);
const isDragging = ref(false);
// "Upload file" opens the familiar drag/drop modal instead of the OS picker.
function triggerUpload() { isOpen.value = false; isUploadModalOpen.value = true; }

function handleDrop(e: any) {
  isDragging.value = false;
  const dropped = Array.from(e.dataTransfer.files).map((file: any) => ({
    id: generateUniqueId(), file, filename: file.name, status: 'processing',
  }));
  allFiles.value.push(...dropped);
  emit('update:uploadedFiles', [...allFiles.value]);
  dropped.forEach((file) => uploadFile(file));
}

// ---- Data loading ----------------------------------------------------------

async function getReportFiles() {
  if (!report_id) return;
  const { data } = await useMyFetch(`/reports/${report_id}/files`, { method: 'GET' });
  // Hide files tagged to a completion (already submitted) and files the agent
  // inherited from a data source (those belong to the agent, not this turn).
  const unused = (data.value || []).filter((file: any) => !file.completion_id && !file.from_data_source);
  allFiles.value = unused.map((file: any) => ({ ...file, status: 'uploaded' }));
}

async function loadRecentUploads() {
  const params = new URLSearchParams({ mine: 'true', source_kind: 'upload', limit: String(RECENT_LIMIT) });
  if (searchTerm.value) params.set('q', searchTerm.value);
  try {
    const { data } = await useMyFetch(`/files?${params.toString()}`, { method: 'GET' });
    const rows = Array.isArray(data.value) ? data.value : [];
    const chosen = new Set(allFiles.value.map((f) => f.id));
    recentUploads.value = rows.filter((f: any) => !chosen.has(f.id));
  } catch { recentUploads.value = []; }
}

async function loadConnections() {
  try {
    const { data } = await useMyFetch('/connections', { method: 'GET' });
    const rows = Array.isArray(data.value) ? data.value : [];
    fileConnections.value = rows
      // Only per-user file connections — never a shared org login.
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
  } catch { fileConnections.value = []; }
}

async function loadDriveFiles(conn: any) {
  if (!conn.connected) return;
  conn.loading = true;
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
    // A superseded generation (the user kept typing) must not clobber newer results.
    if (gen !== searchGeneration) return;
    const body: any = data.value || {};
    if (body.connect_required) { conn.connected = false; conn.files = []; return; }
    const limit = searchTerm.value ? DRIVE_SEARCH_LIMIT : DRIVE_RECENT_LIMIT;
    conn.files = (Array.isArray(body.files) ? body.files : []).slice(0, limit);
  } catch { conn.files = []; }
  finally { conn.loading = false; }
}

async function loadProjectFiles() {
  if (!(props.project as any)?.id) { projectFiles.value = []; return; }
  try {
    const resp = await useMyFetch(`/projects/${(props.project as any).id}`, { method: 'GET' });
    const proj = (resp as any).data?.value;
    projectFiles.value = Array.isArray(proj?.files) ? proj.files : [];
  } catch { projectFiles.value = []; }
}

// ---- Picking ---------------------------------------------------------------

function addChip(file: any) {
  if (allFiles.value.some((f) => f.id === file.id)) return;
  allFiles.value.push({ ...file, status: 'uploaded' });
  emit('update:uploadedFiles', [...allFiles.value]);
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
  } finally { delete picking[file.id]; }
}

async function pickDrive(conn: any, file: any) {
  const key = conn.connection_id + ':' + file.id;
  if (picking[key]) return;
  picking[key] = true;
  try {
    const body: any = { data_source_id: conn.data_source_id, connection_id: conn.connection_id, ref: file.id };
    if (report_id) body.report_id = report_id;
    const { data, error } = await useMyFetch('/files/attach', { method: 'POST', body });
    if (error.value || !data.value) return;
    addChip(data.value);
  } finally { delete picking[key]; }
}

function pickResult(r: any) { r.kind === 'drive' ? pickDrive(r.conn, r.file) : pickExisting(r.file); }

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

// ---- Debounced search ------------------------------------------------------

let searchGeneration = 0;  // bumped once per keystroke so a slow drive can't clobber newer results
let debounceTimer: any = null;
watch(searchQuery, (q) => {
  if (debounceTimer) clearTimeout(debounceTimer);
  debounceTimer = setTimeout(() => {
    searchTerm.value = (q || '').trim();
    searchGeneration++;
    loadRecentUploads();
    // Each drive searches independently and in parallel — a slow one never
    // holds up the others.
    fileConnections.value.forEach((c) => c.connected && loadDriveFiles(c));
  }, 300);
});

watch(isOpen, async (open) => {
  if (!open) { view.value = 'root'; activeDrive.value = null; searchQuery.value = ''; return; }
  await Promise.all([getReportFiles(), loadRecentUploads(), loadConnections(), loadProjectFiles()]);
  // Warm each connected drive's recent list so the root rows drill in instantly.
  fileConnections.value.forEach((c) => c.connected && loadDriveFiles(c));
  emit('update:uploadedFiles', [...allFiles.value]);
});

// ---- Upload from computer (unchanged behavior) -----------------------------

function generateUniqueId() { return `${Date.now()}-${Math.random().toString(36).substr(2, 9)}`; }

function handleFilesUpload(e: any) {
  const selected = Array.from(e.target.files).map((file: any) => ({
    id: generateUniqueId(), file, filename: file.name, status: 'processing',
  }));
  allFiles.value.push(...selected);
  emit('update:uploadedFiles', [...allFiles.value]);
  selected.forEach((file) => uploadFile(file));
}

async function uploadFile(file: any) {
  const formData = new FormData();
  formData.append('file', file.file);
  if (report_id) formData.append('report_id', report_id);
  const idx = allFiles.value.findIndex((f) => f.id === file.id);
  if (idx !== -1) allFiles.value[idx] = { ...allFiles.value[idx], status: 'processing' };
  try {
    const { data, error } = await useMyFetch('/files', { method: 'POST', body: formData });
    if (error.value || !data.value) {
      const i = allFiles.value.findIndex((f) => f.id === file.id);
      if (i !== -1) allFiles.value[i] = { ...allFiles.value[i], status: 'error' };
      return;
    }
    const i = allFiles.value.findIndex((f) => f.id === file.id);
    if (i !== -1) allFiles.value[i] = { ...data.value, status: 'uploaded' };
    emit('update:uploadedFiles', [...allFiles.value]);
  } catch {
    const i = allFiles.value.findIndex((f) => f.id === file.id);
    if (i !== -1) allFiles.value[i] = { ...allFiles.value[i], status: 'error' };
    emit('update:uploadedFiles', [...allFiles.value]);
  }
}

function uploadFilesFromParent(files: FileList | File[]) {
  const arr = Array.from(files).map((file: any) => ({
    id: generateUniqueId(), file, filename: file.name, status: 'processing' as const,
  }));
  allFiles.value.push(...arr);
  emit('update:uploadedFiles', [...allFiles.value]);
  arr.forEach((file) => uploadFile(file));
}

async function removeFile(file: any) {
  allFiles.value = allFiles.value.filter((f) => f !== file);
  if (file.id && report_id) {
    try { await useMyFetch(`/reports/${report_id}/files/${file.id}`, { method: 'DELETE' }); }
    catch (e) { console.error('Error deleting file from server:', e); }
  }
  emit('update:uploadedFiles', [...allFiles.value]);
}

function clearImages() {
  allFiles.value = allFiles.value.filter((f) => {
    const ct = f.content_type || f.type || '';
    return !ct.startsWith('image/');
  });
  emit('update:uploadedFiles', [...allFiles.value]);
}

onMounted(async () => {
  await getReportFiles();
  emit('update:uploadedFiles', [...allFiles.value]);
});

defineExpose({
  refresh: async () => { await getReportFiles(); emit('update:uploadedFiles', [...allFiles.value]); },
  uploadFiles: uploadFilesFromParent,
  removeFile,
  clearImages,
  open: () => { isOpen.value = true; },
});
</script>

<style scoped>
.row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  width: 100%;
  padding: 0.4rem 0.5rem;
  border-radius: 0.375rem;
  cursor: pointer;
  text-align: start;
}
.row:hover { background-color: rgba(0, 0, 0, 0.04); }
:global(.dark) .row:hover { background-color: rgba(255, 255, 255, 0.06); }
.row-ic {
  width: 1.05rem;
  height: 1.05rem;
  flex-shrink: 0;
}
.drop-zone {
  border: 1px dashed #e5e7eb;
  border-radius: 12px;
  transition: all 0.2s ease;
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
