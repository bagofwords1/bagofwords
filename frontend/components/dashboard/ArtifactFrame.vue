<template>
  <div class="h-full w-full flex flex-col bg-white">
    <!-- Header / Toolbar -->
    <div class="flex-shrink-0 flex items-center justify-between px-4 py-2 bg-white border-b">
      <div class="flex items-center gap-3">
        <UTooltip text="Back to chat">
          <button @click="$emit('close')" class="hover:bg-gray-100 p-1 rounded">
            <Icon name="heroicons:x-mark" class="w-4 h-4 text-gray-500" />
          </button>
        </UTooltip>

        <!-- Artifact Selector Dropdown -->
        <div class="flex items-center gap-2">
          <USelectMenu
            v-if="artifactsList.length > 0"
            v-model="selectedArtifactId"
            :options="artifactOptions"
            value-attribute="value"
            option-attribute="label"
            size="xs"
            class="min-w-[280px]"
            placeholder="Select artifact..."
            :ui="{ option: { base: 'py-2' } }"
          >
            <template #label>
              <span class="truncate text-xs">{{ selectedArtifactLabel }}</span>
            </template>
            <template #option="{ option }">
              <div class="flex flex-col gap-0.5 w-full">
                <div class="flex items-center justify-between">
                  <span class="text-xs font-medium text-gray-900 truncate">{{ option.artifact.title || 'Untitled' }}</span>
                  <span class="text-[10px] text-gray-400 ml-2">v{{ option.artifact.version }}</span>
                </div>
                <div class="flex items-center justify-between text-[10px] text-gray-400">
                  <span>{{ formatRelativeTime(option.artifact.created_at) }}</span>
                  <button
                    @click.stop="copyArtifactId(option.artifact.id)"
                    class="hover:text-gray-600 flex items-center gap-0.5 font-mono"
                    title="Click to copy ID"
                  >
                    <Icon name="heroicons:clipboard-document" class="w-3 h-3" />
                    {{ option.artifact.id.slice(0, 8) }}
                  </button>
                </div>
              </div>
            </template>
          </USelectMenu>
          <span v-else class="text-xs text-gray-400 italic">No artifacts yet</span>

          <!-- Use this version button (shown when non-latest is selected) -->
          <button
            v-if="!isLatestSelected && artifactsList.length > 1"
            @click="useThisVersion"
            :disabled="isDuplicating"
            class="text-xs px-2 py-1 bg-blue-50 text-blue-600 hover:bg-blue-100 rounded border border-blue-200 transition-colors disabled:opacity-50 flex items-center gap-1"
          >
            <Spinner v-if="isDuplicating" class="w-3 h-3" />
            <Icon v-else name="heroicons:arrow-uturn-up" class="w-3 h-3" />
            Use this version
          </button>
        </div>
      </div>

      <div class="flex items-center gap-2">
        <span v-if="isLoading" class="text-xs text-gray-400">Loading...</span>
        <span v-else-if="dataReady" class="text-xs text-green-600 flex items-center gap-1">
          <Icon name="heroicons:check-circle" class="w-3 h-3" />
          Ready
        </span>

        <!-- Refresh Dashboard (rerun + refresh) -->
        <UTooltip text="Refresh Data">
          <button
            @click="refreshDashboard"
            :disabled="isRefreshing"
            class="p-1.5 hover:bg-gray-100 rounded transition-colors disabled:opacity-50"
          >
            <Spinner v-if="isRefreshing" class="w-4 h-4 text-gray-500" />
            <Icon v-else name="heroicons:arrow-path" class="w-4 h-4 text-gray-500" />
          </button>
        </UTooltip>

        <!-- Schedule -->
        <CronModal v-if="report" :report="report" />

        <!-- Export PPTX (slides mode only) -->
        <UTooltip v-if="selectedArtifact?.mode === 'slides'" text="Export as PowerPoint">
          <button
            @click="exportPptx"
            :disabled="isExporting"
            class="text-lg items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded disabled:opacity-50"
          >
            <Icon v-if="isExporting" name="heroicons:arrow-path" class="w-4 h-4 text-gray-500 animate-spin" />
            <Icon v-else name="heroicons:arrow-down-tray" class="w-4 h-4 text-purple-600" />
            <span class="text-xs text-purple-600 font-medium">PPTX</span>
          </button>
        </UTooltip>

        <!-- Fullscreen -->
        <UTooltip text="Full screen">
          <button @click="openFullscreen" class="text-lg items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded">
            <Icon name="heroicons:arrows-pointing-out" class="w-4 h-4 text-gray-500" />
          </button>
        </UTooltip>

        <!-- Open in new tab (if published) -->
        <UTooltip text="Open in new tab" v-if="report?.status === 'published'">
          <a :href="`/r/${report.id}`" target="_blank" class="text-lg items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded">
            <Icon name="heroicons:arrow-top-right-on-square" class="w-4 h-4 text-gray-500" />
          </a>
        </UTooltip>

        <!-- Publish -->
        <PublishModal v-if="report" :report="report" />
      </div>
    </div>

    <!-- Iframe Container -->
    <div class="flex-1 min-h-0 relative bg-white">
      <!-- Loading State -->
      <div v-if="isLoading" class="absolute inset-0 flex items-center justify-center bg-white">
        <div class="flex flex-col items-center gap-3">
          <Spinner class="w-6 h-6 text-gray-400" />
          <span class="text-sm text-gray-400">Loading...</span>
        </div>
      </div>

      <!-- Empty State: Has visualizations but no artifact - show Generate Dashboard button -->
      <div v-else-if="!hasArtifact && hasSuccessfulVisualizations" class="absolute inset-0 flex flex-col items-center justify-center bg-white">
        <Icon name="heroicons:sparkles" class="w-8 h-8 text-gray-400 mb-3" />
        <h3 class="text-sm font-medium text-gray-700 mb-1">Ready to create a dashboard</h3>
        <p class="text-xs text-gray-400 mb-4 max-w-xs text-center">
          You have {{ visualizationsData.length }} visualization{{ visualizationsData.length !== 1 ? 's' : '' }} ready
        </p>
        <UButton
          @click="generateDashboardPrompt"
          size="xs"
          color="blue"
        >
          <Icon name="heroicons:bolt" class="w-4 h-4" />
          Generate Dashboard
        </UButton>
      </div>

      <!-- Empty State: No visualizations and no artifact -->
      <div v-else-if="!hasArtifact && !hasVisualizations" class="absolute inset-0 flex flex-col items-center justify-center bg-white">
        <Icon name="heroicons:chart-bar" class="w-6 h-6 text-gray-400 mb-2" />
        <span class="text-sm text-gray-400">No dashboard items yet</span>
      </div>

      <!-- Pending Artifact State (generating) -->
      <div v-else-if="isPendingArtifact" class="absolute inset-0 flex items-center justify-center bg-white">
        <div class="flex flex-col items-center gap-3">
          <Spinner class="w-6 h-6 text-gray-400" />
          <span class="text-sm text-gray-400">
            {{ selectedArtifact?.mode === 'slides' ? 'Generating slides...' : 'Generating dashboard...' }}
          </span>
        </div>
      </div>

      <!-- Slides Mode with Preview Images - Use SlideViewer -->
      <SlideViewer
        v-else-if="hasSlidesWithPreviews && selectedArtifact"
        :artifact-id="selectedArtifact.id"
        class="absolute inset-0"
      />

      <!-- Iframe Render Error State -->
      <div v-else-if="iframeError" class="absolute inset-0 flex flex-col items-center justify-center bg-white">
        <Icon name="heroicons:exclamation-triangle" class="w-8 h-8 text-red-400 mb-3" />
        <h3 class="text-sm font-medium text-gray-700 mb-1">Dashboard failed to render</h3>
        <p class="text-xs text-gray-400 mb-3 max-w-md text-center font-mono bg-gray-50 rounded p-2 border">
          {{ iframeError.length > 200 ? iframeError.slice(0, 200) + '...' : iframeError }}
        </p>
        <UButton
          @click="fixRenderError"
          size="xs"
          color="red"
          variant="soft"
        >
          <Icon name="heroicons:wrench-screwdriver" class="w-4 h-4" />
          Fix Error
        </UButton>
      </div>

      <!-- Iframe (shown when artifact exists and data is ready) -->
      <iframe
        v-show="hasArtifact && !isLoading && !isPendingArtifact && !hasSlidesWithPreviews && !iframeError && iframeSrcdoc"
        ref="iframeRef"
        :srcdoc="iframeSrcdoc"
        sandbox="allow-scripts allow-same-origin"
        class="absolute inset-0 w-full h-full border-0 bg-white z-0"
        @load="onIframeLoad"
      />

      <!-- Polish Mode Button -->
      <div
        v-if="hasArtifact && !isLoading && !isPendingArtifact && !iframeError"
        class="absolute bottom-4 left-4 z-20"
      >
        <UTooltip text="Polish dashboard" :popper="{ placement: 'right' }">
          <button
            @click="togglePolishMode"
            :class="[
              'w-10 h-10 flex items-center justify-center rounded-full shadow-lg transition-all',
              isPolishMode
                ? 'bg-indigo-600 text-white hover:bg-indigo-700 ring-2 ring-indigo-300'
                : 'bg-gray-800 text-gray-100 hover:bg-gray-700'
            ]"
          >
            <Icon name="heroicons:paint-brush" class="w-5 h-5" />
          </button>
        </UTooltip>
      </div>

      <!-- Polish Prompt Box -->
      <div
        v-if="polishPromptVisible"
        class="absolute z-30 w-80 bg-white rounded-lg shadow-xl border border-gray-200 p-3"
        :style="polishPromptPosition"
      >
        <div class="flex items-center gap-2 mb-2">
          <Icon name="heroicons:paint-brush" class="w-3.5 h-3.5 text-indigo-500" />
          <span class="text-xs font-medium text-gray-700">Polish this element</span>
          <button @click="cancelPolishPrompt" class="ml-auto text-gray-400 hover:text-gray-600">
            <Icon name="heroicons:x-mark" class="w-3.5 h-3.5" />
          </button>
        </div>
        <div class="text-[10px] text-gray-400 mb-2 font-mono bg-gray-50 rounded px-2 py-1 truncate">
          &lt;{{ polishSelectedElement?.tag?.toLowerCase() }}&gt; {{ polishSelectedElement?.text?.slice(0, 60) }}
        </div>
        <form @submit.prevent="submitPolishPrompt" class="flex gap-2">
          <input
            ref="polishInputRef"
            v-model="polishInstruction"
            type="text"
            placeholder="e.g. make this bigger, change colors..."
            class="flex-1 text-sm border border-gray-200 rounded-md px-2.5 py-1.5 focus:outline-none focus:ring-1 focus:ring-indigo-400 focus:border-indigo-400"
            @keydown.escape="cancelPolishPrompt"
          />
          <button
            type="submit"
            :disabled="!polishInstruction.trim()"
            class="px-3 py-1.5 bg-indigo-500 text-white text-sm rounded-md hover:bg-indigo-600 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
          >
            Apply
          </button>
        </form>
      </div>
    </div>

    <!-- Fullscreen Modal -->
    <Teleport to="body">
      <UModal v-model="isFullscreenOpen" :ui="{ width: 'sm:max-w-[98vw]', height: 'h-[98vh]' }">
        <div class="h-full flex flex-col">
          <!-- Modal Header -->
          <div class="p-3 flex justify-between items-center border-b bg-white">
            <div class="flex items-center gap-3">
              <span class="text-sm font-medium text-gray-700">{{ selectedArtifact?.title || reportData?.title || 'Artifact' }}</span>
              <span v-if="selectedArtifact" class="text-xs text-gray-400">v{{ selectedArtifact.version }}</span>
            </div>
            <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" @click="closeFullscreen" />
          </div>

          <!-- Modal Content - Full artifact iframe or SlideViewer -->
          <div class="flex-1 min-h-0 relative bg-white">
            <!-- Slides with previews use SlideViewer -->
            <SlideViewer
              v-if="isFullscreenOpen && hasSlidesWithPreviews && selectedArtifact"
              :artifact-id="selectedArtifact.id"
              class="absolute inset-0"
            />
            <!-- Other artifacts use iframe -->
            <iframe
              v-else-if="isFullscreenOpen && iframeSrcdoc"
              :srcdoc="iframeSrcdoc"
              sandbox="allow-scripts allow-same-origin"
              class="absolute inset-0 w-full h-full border-0"
            />
          </div>
        </div>
      </UModal>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch, toRaw, nextTick } from 'vue';
import { useMyFetch } from '~/composables/useMyFetch';
import CronModal from '../CronModal.vue';
import PublishModal from '../PublishModal.vue';
import Spinner from '../Spinner.vue';
import SlideViewer from './SlideViewer.vue';

const toast = useToast();
const config = useRuntimeConfig();
const { token } = useAuth();
const { organization } = useOrganization();

// Format relative time (e.g., "2 hours ago")
function formatRelativeTime(dateString: string): string {
  // Append 'Z' to treat as UTC since backend stores UTC without timezone info
  const date = new Date(dateString.endsWith('Z') ? dateString : dateString + 'Z');
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffSecs = Math.floor(diffMs / 1000);
  const diffMins = Math.floor(diffSecs / 60);
  const diffHours = Math.floor(diffMins / 60);
  const diffDays = Math.floor(diffHours / 24);

  if (diffSecs < 60) return 'just now';
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;
  return date.toLocaleDateString();
}

// Copy artifact ID to clipboard
async function copyArtifactId(id: string) {
  try {
    await navigator.clipboard.writeText(id);
    toast.add({ title: 'Copied', description: 'Artifact ID copied to clipboard', color: 'green' });
  } catch {
    toast.add({ title: 'Failed to copy', color: 'red' });
  }
}

interface ArtifactItem {
  id: string;
  title: string;
  version: number;
  created_at: string;
  mode: string;
  status?: string;
}

const props = defineProps<{
  reportId: string;
  report?: any;
  artifactCode?: string;
}>();

defineEmits<{
  (e: 'close'): void;
}>();

// Fullscreen modal state
const isFullscreenOpen = ref(false);

// Export state
const isExporting = ref(false);

// Refresh state
const isRefreshing = ref(false);

// Iframe render error state
const iframeError = ref<string | null>(null);

// Polish mode state
const isPolishMode = ref(false);
const polishPromptVisible = ref(false);
const polishInstruction = ref('');
const polishInputRef = ref<HTMLInputElement | null>(null);
const polishSelectedElement = ref<{ tag: string; classes: string; text: string; htmlSnippet: string; rect: { top: number; left: number; width: number; height: number } } | null>(null);

const polishPromptPosition = computed(() => {
  if (!polishSelectedElement.value?.rect) return { top: '50%', left: '50%' };
  const r = polishSelectedElement.value.rect;
  // Position below the element, clamped within the container
  const top = Math.min(Math.max(r.top + r.height + 8, 8), 500);
  const left = Math.min(Math.max(r.left, 8), 400);
  return { top: top + 'px', left: left + 'px' };
});

function togglePolishMode() {
  if (isPolishMode.value) {
    exitPolishMode();
  } else {
    enterPolishMode();
  }
}

function enterPolishMode() {
  isPolishMode.value = true;
  polishPromptVisible.value = false;
  polishSelectedElement.value = null;
  polishInstruction.value = '';
  // Tell iframe to enable pick mode
  iframeRef.value?.contentWindow?.postMessage({ type: 'POLISH_ENTER' }, '*');
}

function exitPolishMode() {
  isPolishMode.value = false;
  polishPromptVisible.value = false;
  polishSelectedElement.value = null;
  polishInstruction.value = '';
  // Tell iframe to disable pick mode
  iframeRef.value?.contentWindow?.postMessage({ type: 'POLISH_EXIT' }, '*');
}

function cancelPolishPrompt() {
  polishPromptVisible.value = false;
  polishSelectedElement.value = null;
  polishInstruction.value = '';
  // Re-enter pick mode so user can select another element
  iframeRef.value?.contentWindow?.postMessage({ type: 'POLISH_ENTER' }, '*');
}

function submitPolishPrompt() {
  if (!polishInstruction.value.trim() || !polishSelectedElement.value) return;

  const artifactTitle = selectedArtifact.value?.title || 'the dashboard';
  const artifactId = selectedArtifact.value?.id || selectedArtifactId.value || '';
  const el = polishSelectedElement.value;
  const prompt = `Polish the dashboard "${artifactTitle}" (artifact_id: ${artifactId}).\nTarget element:\n\`\`\`html\n${el.htmlSnippet}\n\`\`\`\nInstruction: ${polishInstruction.value.trim()}`;

  window.dispatchEvent(new CustomEvent('prompt:prefill', {
    detail: { text: prompt, autoSubmit: true }
  }));

  exitPolishMode();
}

// Refresh Dashboard - reruns report queries and refreshes data
async function refreshDashboard() {
  if (isRefreshing.value) return;

  isRefreshing.value = true;
  isLoading.value = true;

  try {
    // Rerun the report (re-execute queries)
    const { error } = await useMyFetch(`/api/reports/${props.reportId}/rerun`, { method: 'POST' });
    if (error.value) throw error.value;

    // Refresh artifact data
    await refreshAll();

    toast.add({ title: 'Dashboard refreshed', color: 'green' });
  } catch (error: any) {
    console.error('Failed to refresh dashboard:', error);
    toast.add({ title: 'Error', description: `Failed to refresh dashboard. ${error.message || ''}`, color: 'red' });
  } finally {
    isRefreshing.value = false;
  }
}

// Open fullscreen modal
function openFullscreen() {
  isFullscreenOpen.value = true;
}

// Close fullscreen modal
function closeFullscreen() {
  isFullscreenOpen.value = false;
}

// Export artifact as PPTX
async function exportPptx() {
  if (!selectedArtifactId.value || isExporting.value) return;

  isExporting.value = true;
  try {
    // Use native fetch for blob download with same auth pattern as useMyFetch
    const headers: Record<string, string> = {
      Authorization: `${token.value}`,
    };
    if (organization.value?.id) {
      headers['X-Organization-Id'] = organization.value.id;
    }

    const response = await fetch(`${config.public.baseURL}/artifacts/${selectedArtifactId.value}/export/pptx`, {
      method: 'GET',
      headers
    });

    if (!response.ok) {
      throw new Error(`HTTP error! status: ${response.status}`);
    }

    const blob = await response.blob();

    // Create download link
    const url = window.URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${selectedArtifact.value?.title || 'presentation'}.pptx`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    window.URL.revokeObjectURL(url);

    toast.add({ title: 'Export complete', description: 'PowerPoint file downloaded successfully.' });
  } catch (error: any) {
    console.error('Failed to export PPTX:', error);
    toast.add({ title: 'Export failed', description: error.message || 'Failed to export PowerPoint file.', color: 'red' });
  } finally {
    isExporting.value = false;
  }
}

const iframeRef = ref<HTMLIFrameElement | null>(null);
const isLoading = ref(true);
const dataReady = ref(false);  // Guards iframeSrcdoc to prevent rendering before data loads
const iframeReady = ref(false);
const visualizationsData = ref<any[]>([]);
const reportData = ref<any>(null);

// Artifact selection state
const artifactsList = ref<ArtifactItem[]>([]);
const selectedArtifactId = ref<string | undefined>(undefined);
const selectedArtifact = ref<any>(null);

// Computed options for dropdown
const artifactOptions = computed(() => {
  return artifactsList.value.map(a => ({
    value: a.id,
    label: `${a.title || 'Untitled'} (v${a.version})`,
    artifact: a
  }));
});

const selectedArtifactLabel = computed(() => {
  const selected = artifactsList.value.find(a => a.id === selectedArtifactId.value);
  if (selected) {
    return `${selected.title || 'Untitled'} (v${selected.version})`;
  }
  return 'Select artifact...';
});

// Check if selected artifact is the latest (first in list, sorted by created_at desc)
const isLatestSelected = computed(() => {
  if (!selectedArtifactId.value || artifactsList.value.length === 0) return true;
  return artifactsList.value[0].id === selectedArtifactId.value;
});

// Check if selected artifact is pending (still generating)
const isPendingArtifact = computed(() => {
  return selectedArtifact.value?.status === 'pending';
});

// Check if any artifacts exist
const hasArtifact = computed(() => {
  return artifactsList.value.length > 0;
});

// Check if visualizations data exists
const hasVisualizations = computed(() => {
  return visualizationsData.value.length > 0;
});

// Check if any visualization has a successful step status
const hasSuccessfulVisualizations = computed(() => {
  return visualizationsData.value.some(viz => viz.stepStatus === 'success');
});

// Check if we have slides mode with preview images (use SlideViewer instead of iframe)
const hasSlidesWithPreviews = computed(() => {
  if (!selectedArtifact.value) return false;
  if (selectedArtifact.value.mode !== 'slides') return false;
  const previewImages = selectedArtifact.value.content?.preview_images;
  return Array.isArray(previewImages) && previewImages.length > 0;
});

// Generate dashboard prompt - dispatches event to update and submit prompt box
function generateDashboardPrompt() {
  const prompt = `Create a dashboard covering the data and visualizations created in this report. Design it with a clean, modern layout and narrative that presents the insights effectively.`;

  // Dispatch custom event to update and auto-submit the prompt box
  window.dispatchEvent(new CustomEvent('prompt:prefill', {
    detail: { text: prompt, autoSubmit: true }
  }));
}

// Fix render error - prefill prompt with error details
function fixRenderError() {
  const errorMsg = iframeError.value || 'Unknown error';
  const artifactTitle = selectedArtifact.value?.title || 'the dashboard';
  const artifactId = selectedArtifact.value?.id || selectedArtifactId.value || '';
  const prompt = `The dashboard "${artifactTitle}" (artifact_id: ${artifactId}) failed to render with this error:\n\`\`\`\n${errorMsg}\n\`\`\`\nPlease fix the artifact code so it renders correctly.`;

  window.dispatchEvent(new CustomEvent('prompt:prefill', {
    detail: { text: prompt, autoSubmit: false }
  }));
}

// State for "Use this version" action
const isDuplicating = ref(false);

// Duplicate the selected artifact to make it the latest/default
async function useThisVersion() {
  if (!selectedArtifactId.value || isDuplicating.value) return;

  isDuplicating.value = true;
  try {
    const { data, error } = await useMyFetch(`/api/artifacts/${selectedArtifactId.value}/duplicate`, {
      method: 'POST'
    });

    if (error.value) throw error.value;

    // Refresh the list and select the new artifact
    await fetchArtifactsList();
    if (data.value && (data.value as any).id) {
      selectedArtifactId.value = (data.value as any).id;
    }

    toast.add({ title: 'Version set as default', color: 'green' });
  } catch (error: any) {
    console.error('Failed to set version as default:', error);
    toast.add({ title: 'Error', description: 'Failed to set version as default.', color: 'red' });
  } finally {
    isDuplicating.value = false;
  }
}

// Handle artifact:select event (select a specific artifact by ID)
function handleArtifactSelect(event: Event) {
  const artifactId = (event as CustomEvent).detail?.artifact_id;
  if (artifactId && artifactsList.value.some(a => a.id === artifactId)) {
    selectedArtifactId.value = artifactId;
  }
}

// Handle artifact:created event (refresh list and select new artifact)
async function handleArtifactCreated(event: Event) {
  const artifactId = (event as CustomEvent).detail?.artifact_id;
  await fetchArtifactsList();
  if (artifactId) {
    selectedArtifactId.value = artifactId;
    // Force refetch in case same artifact transitioned from pending to completed
    await fetchSelectedArtifact();
  }
}

// Load artifacts and data on mount
onMounted(async () => {
  window.addEventListener('message', handleIframeMessage);
  window.addEventListener('artifact:select', handleArtifactSelect);
  window.addEventListener('artifact:created', handleArtifactCreated);

  // First fetch artifact list to know which artifact is selected
  await fetchArtifactsList();

  // Then fetch visualization data filtered by the selected artifact (if any)
  await fetchData(selectedArtifactId.value);
});

// Fetch list of all artifacts for the report
async function fetchArtifactsList() {
  try {
    const { data } = await useMyFetch(`/artifacts/report/${props.reportId}`);
    if (data.value && Array.isArray(data.value)) {
      artifactsList.value = data.value as ArtifactItem[];

      // Auto-select the most recent artifact
      if (artifactsList.value.length > 0) {
        selectedArtifactId.value = artifactsList.value[0].id;
        await fetchSelectedArtifact();
      }
    }
  } catch (e) {
    console.log('[ArtifactFrame] No artifacts found');
  }
}

// Fetch the full artifact content when selection changes
async function fetchSelectedArtifact() {
  if (!selectedArtifactId.value) {
    selectedArtifact.value = null;
    return;
  }

  try {
    const { data } = await useMyFetch(`/api/artifacts/${selectedArtifactId.value}`);
    if (data.value) {
      selectedArtifact.value = data.value;
      console.log('[ArtifactFrame] Loaded artifact:', (data.value as any).title);
      // Broadcast active artifact viz IDs so ToolWidgetPreview can show "Added to Dashboard"
      const vizIds = (data.value as any)?.content?.visualization_ids || [];
      window.dispatchEvent(new CustomEvent('artifact:viz-ids', { detail: { visualization_ids: vizIds } }));
    }
  } catch (e) {
    console.error('[ArtifactFrame] Failed to fetch artifact:', e);
  }
}

// Watch for artifact selection changes - refetch data filtered by new artifact
watch(selectedArtifactId, async (newId, oldId) => {
  iframeError.value = null;
  iframeReady.value = false;
  if (isPolishMode.value) exitPolishMode();
  await fetchSelectedArtifact();
  // Only refetch data if this is a user-initiated change (not initial load)
  if (oldId !== undefined) {
    await fetchData(newId);
  }
});

onUnmounted(() => {
  window.removeEventListener('message', handleIframeMessage);
  window.removeEventListener('artifact:select', handleArtifactSelect);
  window.removeEventListener('artifact:created', handleArtifactCreated);
  if (isPolishMode.value) exitPolishMode();
});

// Handle messages from iframe
function handleIframeMessage(event: MessageEvent) {
  if (event.data?.type === 'ARTIFACT_READY') {
    console.log('[ArtifactFrame] Iframe ready');
    iframeError.value = null;
    iframeReady.value = true;
    sendDataToIframe();
  } else if (event.data?.type === 'ARTIFACT_ERROR') {
    console.error('[ArtifactFrame] Iframe render error:', event.data.payload?.message);
    iframeError.value = event.data.payload?.message || 'Unknown render error';
  } else if (event.data?.type === 'POLISH_ELEMENT_SELECTED') {
    polishSelectedElement.value = event.data.element;
    polishPromptVisible.value = true;
    polishInstruction.value = '';
    nextTick(() => polishInputRef.value?.focus());
  }
}

// Send data to iframe via postMessage
function sendDataToIframe() {
  if (!iframeRef.value?.contentWindow || !iframeReady.value) return;

  const payload = JSON.parse(JSON.stringify({
    report: toRaw(reportData.value),
    visualizations: toRaw(visualizationsData.value)
  }));

  try {
    iframeRef.value.contentWindow.postMessage({
      type: 'ARTIFACT_DATA',
      payload
    }, '*');
  } catch (err: any) {
    console.error('[ArtifactFrame] Failed to send data to iframe:', err);
    iframeError.value = err?.message || 'Failed to send data to dashboard iframe';
    return;
  }

  dataReady.value = true;
  console.log('[ArtifactFrame] Data sent to iframe:', visualizationsData.value.length, 'visualizations');
}

// Fetch visualization data for the report (optionally filtered by artifact)
async function fetchData(artifactId?: string) {
  isLoading.value = true;
  dataReady.value = false;

  try {
    // Fetch report info
    const { data: reportRes } = await useMyFetch(`/api/reports/${props.reportId}`);
    if (reportRes.value) {
      reportData.value = {
        id: (reportRes.value as any).id,
        title: (reportRes.value as any).title,
        theme: (reportRes.value as any).theme_name || (reportRes.value as any).report_theme_name
      };
    }

    // Fetch queries with visualizations - filter by artifact_id if provided
    const queryParams = artifactId ? `?report_id=${props.reportId}&artifact_id=${artifactId}` : `?report_id=${props.reportId}`;
    const { data: queriesRes } = await useMyFetch(`/api/queries${queryParams}`);
    const queries = Array.isArray(queriesRes.value) ? queriesRes.value : [];

    // Build visualization data array
    const vizData: any[] = [];

    for (const query of queries) {
      // Fetch default step for this query
      const { data: stepRes } = await useMyFetch(`/api/queries/${query.id}/default_step`);
      const step = (stepRes.value as any)?.step;

      // Process each visualization in the query
      for (const viz of query.visualizations || []) {
        vizData.push({
          id: viz.id,
          title: viz.title || query.title || 'Untitled',
          view: viz.view || {},
          rows: step?.data?.rows || [],
          columns: step?.data?.columns || [],
          dataModel: step?.data_model || {},
          stepStatus: step?.status
        });
      }
    }

    visualizationsData.value = vizData;
    console.log('[ArtifactFrame] Fetched', vizData.length, 'visualizations');

    // Mark data as ready - triggers iframeSrcdoc to compute with loaded data
    dataReady.value = true;

  } catch (e) {
    console.error('[ArtifactFrame] Failed to fetch data:', e);
  } finally {
    isLoading.value = false;
    if (iframeReady.value) {
      sendDataToIframe();
    }
  }
}

// Refresh everything
async function refreshAll() {
  await fetchArtifactsList();
  await fetchData(selectedArtifactId.value);
}

// Called when iframe loads
function onIframeLoad() {
  // Iframe loaded, but we wait for ARTIFACT_READY message
}

// Sample React code for when no artifact exists
const sampleArtifactCode = computed(() => {
  const SC = '</' + 'script>';
  return `
<script type="text/babel">
// Default Artifact - Create one with the agent!
function App() {
  const data = useArtifactData();

  if (!data) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="animate-pulse text-gray-400">Loading...</div>
      </div>
    );
  }

  const { report, visualizations } = data;

  return (
    <div className="min-h-full bg-gradient-to-br from-slate-50 to-slate-100 p-8">
      {/* Header */}
      <div className="mb-8">
        <h1 className="text-3xl font-bold text-gray-900 tracking-tight">
          {report?.title || 'Dashboard'}
        </h1>
        <p className="text-sm text-gray-500 mt-2">
          {visualizations.length} visualization{visualizations.length !== 1 ? 's' : ''} available
        </p>
      </div>

      {/* Empty state */}
      {visualizations.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-gray-100 rounded-full flex items-center justify-center mb-4">
            <svg className="w-8 h-8 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 17v-2m3 2v-4m3 4v-6m2 10H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
            </svg>
          </div>
          <h3 className="text-lg font-medium text-gray-900 mb-1">No visualizations yet</h3>
          <p className="text-sm text-gray-500 max-w-sm">
            Ask the agent to create visualizations, then generate an artifact to see them here.
          </p>
        </div>
      ) : (
        /* Grid of visualizations */
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {visualizations.map((viz) => (
            <VisualizationCard key={viz.id} viz={viz} />
          ))}
        </div>
      )}
    </div>
  );
}

function VisualizationCard({ viz }) {
  const chartRef = React.useRef(null);
  const chartInstance = React.useRef(null);

  React.useEffect(() => {
    if (!chartRef.current || !viz.rows?.length) return;

    if (chartInstance.current) {
      chartInstance.current.dispose();
    }

    const chart = echarts.init(chartRef.current);
    chartInstance.current = chart;

    const options = buildChartOptions(viz);
    if (options) {
      chart.setOption(options);
    }

    const resizeHandler = () => chart.resize();
    window.addEventListener('resize', resizeHandler);

    return () => {
      window.removeEventListener('resize', resizeHandler);
      chart.dispose();
    };
  }, [viz]);

  const viewType = viz.view?.view?.type || viz.view?.type || viz.dataModel?.type || 'table';

  return (
    <div className="bg-white rounded-2xl shadow-sm border border-gray-100 overflow-hidden hover:shadow-md transition-shadow">
      <div className="px-5 py-4 border-b border-gray-50">
        <h3 className="font-semibold text-gray-900">{viz.title}</h3>
        <span className="text-xs text-gray-400 uppercase tracking-wide">{viewType}</span>
      </div>
      <div className="p-5">
        {viz.rows?.length > 0 ? (
          viewType === 'table' ? (
            <TableView data={viz} />
          ) : (
            <div ref={chartRef} className="h-72 w-full" />
          )
        ) : (
          <div className="h-72 flex items-center justify-center text-gray-400">
            No data available
          </div>
        )}
      </div>
      <div className="px-5 py-3 bg-gray-50/50 text-xs text-gray-500">
        {viz.rows?.length || 0} rows
      </div>
    </div>
  );
}

function TableView({ data }) {
  const { rows, columns } = data;
  const cols = columns?.length
    ? columns.map(c => c.field || c.colId || c.headerName)
    : Object.keys(rows[0] || {});

  return (
    <div className="overflow-x-auto max-h-72 rounded-lg border border-gray-100">
      <table className="w-full text-sm">
        <thead className="bg-gray-50 sticky top-0">
          <tr>
            {cols.slice(0, 6).map((col) => (
              <th key={col} className="text-left px-3 py-2 font-medium text-gray-600 border-b border-gray-100">
                {col}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 10).map((row, i) => (
            <tr key={i} className="border-b border-gray-50 hover:bg-gray-50/50">
              {cols.slice(0, 6).map((col) => (
                <td key={col} className="px-3 py-2 text-gray-700">
                  {formatValue(row[col] ?? row[col.toLowerCase()])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > 10 && (
        <div className="text-xs text-gray-400 p-2 text-center bg-gray-50">
          Showing 10 of {rows.length} rows
        </div>
      )}
    </div>
  );
}

function formatValue(val) {
  if (val === null || val === undefined) return '-';
  if (typeof val === 'number') return val.toLocaleString();
  return String(val);
}

function buildChartOptions(viz) {
  const { rows, view, dataModel } = viz;
  if (!rows?.length) return null;

  const type = (view?.view?.type || view?.type || dataModel?.type || '').toLowerCase();
  const colors = view?.view?.palette?.colors || ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6'];

  const normalizedRows = rows.map(r => {
    const o = {};
    Object.keys(r).forEach(k => o[k.toLowerCase()] = r[k]);
    return o;
  });

  const series = dataModel?.series?.[0] || {};
  const categoryKey = (view?.view?.x || series.key || Object.keys(normalizedRows[0])[0])?.toLowerCase();
  const valueKey = (view?.view?.y || series.value || Object.keys(normalizedRows[0])[1])?.toLowerCase();

  if (!categoryKey) return null;

  const categories = [...new Set(normalizedRows.map(r => String(r[categoryKey] || '')))];
  const values = categories.map(cat => {
    const row = normalizedRows.find(r => String(r[categoryKey]) === cat);
    const v = row ? Number(row[valueKey]) : 0;
    return isNaN(v) ? 0 : v;
  });

  if (type === 'pie_chart' || type === 'pie') {
    return {
      tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
      series: [{
        type: 'pie',
        radius: ['45%', '75%'],
        center: ['50%', '50%'],
        data: categories.map((name, i) => ({
          name,
          value: values[i],
          itemStyle: { color: colors[i % colors.length] }
        })),
        label: { show: false },
        emphasis: { label: { show: true, fontSize: 14, fontWeight: 'bold' } }
      }]
    };
  }

  if (type === 'bar_chart' || type === 'bar' || !type || type === 'table') {
    return {
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      grid: { left: 50, right: 20, bottom: 50, top: 20, containLabel: true },
      xAxis: {
        type: 'category',
        data: categories,
        axisLabel: { rotate: categories.length > 6 ? 45 : 0, fontSize: 11, color: '#6b7280' },
        axisLine: { lineStyle: { color: '#e5e7eb' } }
      },
      yAxis: {
        type: 'value',
        axisLine: { show: false },
        splitLine: { lineStyle: { color: '#f3f4f6' } }
      },
      series: [{
        type: 'bar',
        data: values,
        itemStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: colors[0] },
            { offset: 1, color: colors[0] + '80' }
          ]),
          borderRadius: [6, 6, 0, 0]
        },
        barMaxWidth: 50
      }]
    };
  }

  if (type === 'line_chart' || type === 'line' || type === 'area_chart' || type === 'area') {
    const isArea = type.includes('area');
    return {
      tooltip: { trigger: 'axis' },
      grid: { left: 50, right: 20, bottom: 50, top: 20, containLabel: true },
      xAxis: {
        type: 'category',
        data: categories,
        axisLabel: { rotate: categories.length > 6 ? 45 : 0, fontSize: 11, color: '#6b7280' },
        axisLine: { lineStyle: { color: '#e5e7eb' } }
      },
      yAxis: {
        type: 'value',
        axisLine: { show: false },
        splitLine: { lineStyle: { color: '#f3f4f6' } }
      },
      series: [{
        type: 'line',
        data: values,
        smooth: true,
        symbol: 'circle',
        symbolSize: 6,
        itemStyle: { color: colors[0] },
        lineStyle: { width: 3 },
        areaStyle: isArea ? {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: colors[0] + '40' },
            { offset: 1, color: colors[0] + '05' }
          ])
        } : undefined
      }]
    };
  }

  return null;
}

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(<App />);
${SC}
`;
});

// Build the full iframe srcdoc with embedded data
// Guard: only compute once ALL data is ready to prevent iframe loading with empty data
const iframeSrcdoc = computed(() => {
  // Wait for visualization data to be loaded
  if (!dataReady.value) return undefined;

  // If artifacts exist, wait for the selected artifact to be fully loaded
  if (artifactsList.value.length > 0 && !selectedArtifact.value?.content?.code) return undefined;

  const embeddedData = JSON.stringify({
    report: reportData.value,
    visualizations: visualizationsData.value
  });

  // Priority: props > selected artifact from DB > sample code
  const artifactCode = props.artifactCode
    || selectedArtifact.value?.content?.code
    || sampleArtifactCode.value;

  const artifactMode = selectedArtifact.value?.mode || 'page';
  const SC = '</' + 'script>';

  // Slides mode: Pure HTML + Tailwind (no React/Babel)
  if (artifactMode === 'slides') {
    return `<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <script src="/libs/tailwindcss-3.4.16.js">${SC}
  <style>
    html, body { height: 100%; margin: 0; padding: 0; }
    body { font-family: system-ui, -apple-system, sans-serif; }
    .slide { transition: opacity 0.3s ease-in-out; }
  </style>
</head>
<body class="bg-slate-900">
  <script>
    window.ARTIFACT_DATA = ${embeddedData};
    console.log('[Slides] Data loaded:', window.ARTIFACT_DATA?.visualizations?.length || 0, 'visualizations');
  ${SC}

  ${artifactCode}
</body>
</html>`;
  }

  // Dashboard mode: React + Babel + ECharts
  return `<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <script src="/libs/tailwindcss-3.4.16.js">${SC}
  <script crossorigin src="/libs/react-18.development.js">${SC}
  <script crossorigin src="/libs/react-dom-18.development.js">${SC}
  <script src="/libs/babel-standalone.min.js">${SC}
  <script src="/libs/echarts-5.min.js">${SC}
  <script src="/libs/react-is-18.production.min.js">${SC}
  <script src="/libs/recharts-3.8.1.min.js">${SC}
  <script>if(window.Recharts)Object.assign(window,Recharts);${SC}
  <style>
    html, body, #root { height: 100%; margin: 0; padding: 0; }
    body { font-family: system-ui, -apple-system, sans-serif; }
  </style>
</head>
<body>
  <div id="root"><div style="display:flex;align-items:center;justify-content:center;height:100%;color:#9ca3af;">Loading artifact...</div></div>

  <script>
    window.ARTIFACT_DATA = ${embeddedData};
    window.useArtifactData = function() {
      return window.ARTIFACT_DATA;
    };
    // Global LoadingSpinner component for artifact code
    window.LoadingSpinner = function(props) {
      var size = props && props.size ? props.size : 24;
      return React.createElement('svg', {
        xmlns: 'http://www.w3.org/2000/svg',
        width: size,
        height: size,
        viewBox: '0 0 24 24',
        className: props && props.className ? props.className : ''
      },
        React.createElement('path', {
          fill: 'currentColor',
          d: 'M12 2A10 10 0 1 0 22 12A10 10 0 0 0 12 2Zm0 18a8 8 0 1 1 8-8A8 8 0 0 1 12 20Z',
          opacity: '0.5'
        }),
        React.createElement('path', {
          fill: 'currentColor',
          d: 'M20 12h2A10 10 0 0 0 12 2V4A8 8 0 0 1 20 12Z'
        },
          React.createElement('animateTransform', {
            attributeName: 'transform',
            dur: '1s',
            from: '0 12 12',
            repeatCount: 'indefinite',
            to: '360 12 12',
            type: 'rotate'
          })
        )
      );
    };
    // ── Global filter store (shared state for useFilters hook) ──
    window.__filterStore = (function() {
      var filters = {};
      var listeners = [];
      return {
        get: function() { return filters; },
        set: function(field, value) {
          var next = {};
          for (var k in filters) next[k] = filters[k];
          if (value == null || value === '') delete next[field];
          else next[field] = value;
          filters = next;
          for (var i = 0; i < listeners.length; i++) listeners[i]();
        },
        reset: function() {
          filters = {};
          for (var i = 0; i < listeners.length; i++) listeners[i]();
        },
        sub: function(fn) {
          listeners.push(fn);
          return function() {
            var idx = listeners.indexOf(fn);
            if (idx >= 0) listeners.splice(idx, 1);
          };
        }
      };
    })();

    // ── useFilters() hook — cross-visualization filtering ──
    // No magic column detection — LLM explicitly chooses which columns to filter.
    // filterRows(rows, fieldMap?) applies active filters; optional fieldMap
    // remaps canonical filter keys to viz-specific column names,
    // e.g. filterRows(rows, { country: 'CountryName' })
    window.useFilters = function() {
      var _s = React.useState(0);
      var forceUpdate = _s[1];

      React.useEffect(function() {
        return window.__filterStore.sub(function() {
          forceUpdate(function(c) { return c + 1; });
        });
      }, []);

      var filters = window.__filterStore.get();

      var filterRows = React.useCallback(function(rows, fieldMap) {
        var entries = Object.entries(filters);
        if (!entries.length) return rows;
        return rows.filter(function(row) {
          for (var i = 0; i < entries.length; i++) {
            var key = entries[i][0], val = entries[i][1];
            var col = (fieldMap && fieldMap[key]) ? fieldMap[key] : key;
            if (!Object.prototype.hasOwnProperty.call(row, col)) continue;
            var rv = row[col];
            if (val && typeof val === 'object' && !Array.isArray(val) && (val.from || val.to)) {
              var s = String(rv);
              if (val.from && s < val.from) return false;
              if (val.to && s > val.to) return false;
            } else if (Array.isArray(val)) {
              if (val.length > 0 && val.indexOf(String(rv)) === -1) return false;
            } else {
              if (val && String(rv).toLowerCase().indexOf(String(val).toLowerCase()) === -1) return false;
            }
          }
          return true;
        });
      }, [filters]);

      return {
        filters: filters,
        setFilter: window.__filterStore.set,
        resetFilters: window.__filterStore.reset,
        filterRows: filterRows
      };
    };
    // Global fmt() number formatter
    window.fmt = function(n, opts) {
      if (n == null) return '\u2014';
      if (typeof n !== 'number') return String(n);
      opts = opts || {};
      if (opts.currency) return new Intl.NumberFormat('en-US', { style: 'currency', currency: opts.currency === true ? 'USD' : opts.currency, maximumFractionDigits: opts.decimals != null ? opts.decimals : 0 }).format(n);
      if (opts.pct) return n.toFixed(1) + '%';
      if (Math.abs(n) >= 1e9) return (n/1e9).toFixed(1) + 'B';
      if (Math.abs(n) >= 1e6) return (n/1e6).toFixed(1) + 'M';
      if (Math.abs(n) >= 1e3) return (n/1e3).toFixed(1) + 'K';
      return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
    };
    // Global CustomTooltip for Recharts
    window.CustomTooltip = function(props) {
      if (!props.active || !props.payload || !props.payload.length) return null;
      var h = React.createElement;
      return h('div', { className: 'bg-slate-900 text-white px-4 py-3 rounded-xl shadow-xl border border-slate-700/50 text-sm' }, [
        h('p', { key: 'l', className: 'font-medium text-slate-300 mb-1' }, props.label),
      ].concat(props.payload.map(function(p, i) {
        return h('p', { key: i, className: 'flex items-center gap-2' }, [
          h('span', { key: 'd', className: 'w-2 h-2 rounded-full inline-block', style: { backgroundColor: p.color } }),
          h('span', { key: 'n', className: 'text-slate-400' }, p.name + ': '),
          h('span', { key: 'v', className: 'font-semibold' }, typeof p.value === 'number' ? p.value.toLocaleString() : p.value),
        ]);
      })));
    };
    // Global KPICard component
    window.KPICard = function(props) {
      var h = React.createElement;
      var color = props.color || '#3B82F6';
      var theme = props.className || 'bg-white border-slate-200 text-slate-900';
      var titleCls = props.titleClassName || 'text-slate-500';
      var subtitleCls = props.subtitleClassName || 'text-slate-500';
      return h('div', { className: 'relative rounded-2xl border p-5 shadow-sm overflow-hidden ' + theme }, [
        h('div', { key: 'bar', className: 'absolute inset-x-0 top-0 h-1', style: { background: 'linear-gradient(90deg, ' + color + ', ' + color + '99)' } }),
        h('p', { key: 't', className: 'text-xs font-medium uppercase tracking-wider mb-1 ' + titleCls }, props.title),
        h('p', { key: 'v', className: 'text-2xl font-semibold' }, props.value),
        props.subtitle ? h('p', { key: 's', className: 'text-sm mt-1 ' + subtitleCls }, props.subtitle) : null,
      ]);
    };
    // Global SectionCard wrapper
    window.SectionCard = function(props) {
      var h = React.createElement;
      var theme = props.className || 'bg-white border-slate-200';
      var titleCls = props.titleClassName || 'text-slate-800';
      var subtitleCls = props.subtitleClassName || 'text-slate-500';
      return h('div', { className: 'rounded-2xl border shadow-sm p-6 ' + theme }, [
        props.title ? h('div', { key: 'hdr', className: 'mb-4' }, [
          h('h2', { key: 't', className: 'text-lg font-semibold ' + titleCls }, props.title),
          props.subtitle ? h('p', { key: 's', className: 'text-sm mt-1 ' + subtitleCls }, props.subtitle) : null,
        ]) : null,
        h('div', { key: 'body' }, props.children),
      ]);
    };
    // Global FilterSelect — multi-select dropdown with checkboxes
    // options: string[] OR {label, value}[]
    window.FilterSelect = function(props) {
      var h = React.createElement;
      var label = props.label || '';
      var rawOpts = props.options || [];
      var opts = rawOpts.map(function(o) { return typeof o === 'object' && o !== null ? { val: o.value, lbl: o.label || String(o.value) } : { val: o, lbl: String(o) }; });
      var selected = props.selected || [];
      var onChange = props.onChange || function(){};
      var theme = props.className || 'bg-white border-slate-200 text-slate-900';
      var searchable = props.searchable !== undefined ? props.searchable : opts.length >= 8;
      var _s = React.useState(false), open = _s[0], setOpen = _s[1];
      var _q = React.useState(''), query = _q[0], setQuery = _q[1];
      var ref = React.useRef(null);
      var searchRef = React.useRef(null);
      React.useEffect(function() {
        function handleClick(e) { if (ref.current && !ref.current.contains(e.target)) setOpen(false); }
        document.addEventListener('mousedown', handleClick);
        return function() { document.removeEventListener('mousedown', handleClick); };
      }, []);
      React.useEffect(function() {
        if (open && searchable && searchRef.current) searchRef.current.focus();
        if (!open) setQuery('');
      }, [open]);
      function toggle(val) {
        var idx = selected.indexOf(val);
        onChange(idx >= 0 ? selected.filter(function(v){ return v !== val; }) : selected.concat([val]));
      }
      var filtered = searchable && query
        ? opts.filter(function(o) { return o.lbl.toLowerCase().indexOf(query.toLowerCase()) !== -1; })
        : opts;
      var selLabels = opts.filter(function(o) { return selected.indexOf(o.val) >= 0; }).map(function(o) { return o.lbl; });
      var display = selected.length === 0 ? 'All' : selLabels.length <= 2 ? selLabels.join(', ') : selected.length + ' selected';
      return h('div', { ref: ref, className: 'relative inline-block min-w-[140px]' }, [
        label ? h('label', { key: 'l', className: 'block text-xs font-medium opacity-60 mb-1' }, label) : null,
        h('button', {
          key: 'btn', type: 'button',
          className: 'w-full flex items-center justify-between gap-2 rounded-lg border px-3 py-1.5 text-sm cursor-pointer ' + theme,
          onClick: function() { setOpen(!open); }
        }, [
          h('span', { key: 't', className: 'truncate' }, display),
          h('svg', { key: 'i', width: 12, height: 12, viewBox: '0 0 12 12', className: 'opacity-50 shrink-0' },
            h('path', { d: 'M3 5l3 3 3-3', stroke: 'currentColor', strokeWidth: 1.5, fill: 'none' }))
        ]),
        open ? h('div', {
          key: 'dd',
          className: 'absolute z-50 mt-1 left-0 right-0 rounded-lg border shadow-lg max-h-72 overflow-auto py-1 ' + theme,
          style: { backgroundColor: '#fff' }
        }, [
          searchable ? h('div', { key: 'search', className: 'px-2 pt-1 pb-1 sticky top-0', style: { backgroundColor: '#fff' } }, [
            h('input', {
              ref: searchRef, type: 'text', value: query,
              placeholder: 'Search...',
              onChange: function(e) { setQuery(e.target.value); },
              className: 'w-full rounded border px-2 py-1 text-sm outline-none focus:border-blue-400 ' + theme,
              onClick: function(e) { e.stopPropagation(); }
            })
          ]) : null,
          selected.length > 0 ? h('button', {
            key: 'clr', type: 'button',
            className: 'w-full text-left px-3 py-1.5 text-xs font-medium opacity-50 hover:opacity-100',
            onClick: function() { onChange([]); }
          }, 'Clear all') : null
        ].concat(filtered.map(function(o) {
          var isSelected = selected.indexOf(o.val) >= 0;
          return h('label', {
            key: o.val,
            className: 'flex items-center gap-2 px-3 py-1.5 text-sm cursor-pointer hover:bg-black/5'
          }, [
            h('input', {
              key: 'cb', type: 'checkbox', checked: isSelected,
              onChange: function() { toggle(o.val); },
              className: 'rounded border-slate-300 accent-blue-500'
            }),
            h('span', { key: 'v', className: 'truncate' }, o.lbl)
          ]);
        }))) : null
      ]);
    };
    // Global FilterSearch — text search input for columns with unique values
    // onChange receives the raw DOM event (like a native <input>) so LLM code
    // can use the standard pattern: onChange={e => setFilter(field, e.target.value)}
    window.FilterSearch = function(props) {
      var h = React.createElement;
      var label = props.label || '';
      var value = props.value || '';
      var onChange = props.onChange || function(){};
      var placeholder = props.placeholder || 'Search...';
      var theme = props.className || 'bg-white border-slate-200 text-slate-900';
      return h('div', { className: 'inline-block min-w-[140px]' }, [
        label ? h('label', { key: 'l', className: 'block text-xs font-medium opacity-60 mb-1' }, label) : null,
        h('input', {
          key: 'inp',
          type: 'text',
          value: value,
          placeholder: placeholder,
          onChange: onChange,
          className: 'w-full rounded-lg border px-3 py-1.5 text-sm ' + theme
        })
      ]);
    };
    // Global FilterDateRange — two date inputs for date/time column filtering
    // onChange receives { from, to } object — pass to setFilter(field, { from, to })
    window.FilterDateRange = function(props) {
      var h = React.createElement;
      var label = props.label || '';
      var value = props.value || {};
      var onChange = props.onChange || function(){};
      var theme = props.className || 'bg-white border-slate-200 text-slate-900';
      var inputType = props.type || 'date';
      return h('div', { className: 'inline-block min-w-[200px]' }, [
        label ? h('label', { key: 'l', className: 'block text-xs font-medium opacity-60 mb-1' }, label) : null,
        h('div', { key: 'row', className: 'flex items-center gap-2' }, [
          h('input', {
            key: 'from', type: inputType, value: value.from || '',
            onChange: function(e) { onChange({ from: e.target.value || null, to: value.to || null }); },
            className: 'w-full rounded-lg border px-2 py-1.5 text-sm ' + theme
          }),
          h('span', { key: 'sep', className: 'text-xs opacity-50' }, '–'),
          h('input', {
            key: 'to', type: inputType, value: value.to || '',
            onChange: function(e) { onChange({ from: value.from || null, to: e.target.value || null }); },
            className: 'w-full rounded-lg border px-2 py-1.5 text-sm ' + theme
          })
        ])
      ]);
    };
    // Expose React hooks as globals so LLM code can use them without React. prefix
    window.useState = React.useState;
    window.useEffect = React.useEffect;
    window.useRef = React.useRef;
    window.useMemo = React.useMemo;
    window.useCallback = React.useCallback;

    // Register 'bow' ECharts theme — clean defaults so LLM only writes data mapping
    echarts.registerTheme('bow', {
      color: ['#3B82F6', '#10B981', '#8B5CF6', '#F59E0B', '#EF4444', '#06B6D4', '#EC4899', '#14B8A6', '#60A5FA', '#34D399'],
      backgroundColor: 'transparent',
      categoryAxis: {
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: '#64748b', fontSize: 12 },
        splitLine: { show: false }
      },
      valueAxis: {
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: '#64748b', fontSize: 12 },
        splitLine: { lineStyle: { color: '#f1f5f9' } }
      },
      line: { smooth: true, symbol: 'none', lineStyle: { width: 2 } },
      bar: { itemStyle: { borderRadius: [6, 6, 0, 0] } },
      pie: { itemStyle: { borderRadius: 6 } },
      grid: { left: 40, right: 20, top: 20, bottom: 40, containLabel: true },
      tooltip: {
        backgroundColor: 'rgba(15, 23, 42, 0.95)',
        borderColor: 'rgba(51, 65, 85, 0.5)',
        borderWidth: 1,
        borderRadius: 12,
        padding: [12, 16],
        textStyle: { color: '#fff', fontSize: 13 },
        trigger: 'axis'
      }
    });

    // Global EChart React wrapper — handles init/dispose/resize automatically
    window.EChart = function(props) {
      var ref = React.useRef(null);
      var chartRef = React.useRef(null);
      var h = props.height || 400;
      React.useEffect(function() {
        if (!ref.current) return;
        var chart = echarts.init(ref.current, 'bow');
        chartRef.current = chart;
        if (props.option) chart.setOption(props.option);
        var ro = new ResizeObserver(function() { chart.resize(); });
        ro.observe(ref.current);
        return function() { ro.disconnect(); chart.dispose(); };
      }, []);
      React.useEffect(function() {
        if (chartRef.current && props.option) {
          chartRef.current.setOption(props.option, true);
        }
      }, [props.option]);
      return React.createElement('div', {
        ref: ref,
        style: { width: '100%', height: h },
        className: props.className || ''
      });
    };

    // Fix ECharts 0-height issue: resize all charts after render
    window.resizeAllCharts = function() {
      if (typeof echarts !== 'undefined') {
        var charts = document.querySelectorAll('[_echarts_instance_]');
        charts.forEach(function(el) {
          var chart = echarts.getInstanceByDom(el);
          if (chart) chart.resize();
        });
      }
    };
    // Auto-resize after React renders
    setTimeout(window.resizeAllCharts, 100);
    setTimeout(window.resizeAllCharts, 500);
    window.addEventListener('resize', window.resizeAllCharts);
    console.log('[Artifact] Data loaded:', window.ARTIFACT_DATA?.visualizations?.length || 0, 'visualizations');

    // Polish mode: element pick, highlight & custom cursor
    (function() {
      var polishActive = false;
      var currentHighlight = null;

      // Styles: highlight outline + custom cursor pill + hide native cursor
      var polishStyle = document.createElement('style');
      polishStyle.textContent = [
        '.__polish-highlight { outline: 2px solid #6366f1 !important; outline-offset: 2px; }',
        '.__polish-active { cursor: crosshair !important; }',
        '.__polish-active * { cursor: crosshair !important; }',
        '.__polish-cursor { position: fixed; pointer-events: none; z-index: 99999; display: none; }',
        '.__polish-cursor-inner { display: flex; align-items: center; gap: 6px; background: #4f46e5; color: white; font-size: 12px; font-weight: 500; font-family: system-ui, sans-serif; padding: 5px 10px 5px 8px; border-radius: 20px; box-shadow: 0 4px 12px rgba(79,70,229,0.35); white-space: nowrap; }'
      ].join('\\n');
      document.head.appendChild(polishStyle);

      // Create custom cursor element
      var cursorEl = document.createElement('div');
      cursorEl.className = '__polish-cursor';
      cursorEl.innerHTML = '<div class="__polish-cursor-inner"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18.37 2.63 14 7l-1.59-1.59a2 2 0 0 0-2.82 0L8 7l9 9 1.59-1.59a2 2 0 0 0 0-2.82L17 10l4.37-4.37a2.12 2.12 0 1 0-3-3Z"/><path d="M9 8c-2 3-4 3.5-7 4l8 10c2-1 6-5 6-7"/><path d="M14.5 17.5 4.5 15"/></svg>Click to select</div>';
      document.body.appendChild(cursorEl);

      function onMouseMove(e) {
        cursorEl.style.left = (e.clientX + 12) + 'px';
        cursorEl.style.top = (e.clientY + 12) + 'px';
      }

      function snapToMeaningful(el) {
        // If the element itself is a heading, paragraph, table, list, or image — it's already meaningful
        var selfTag = (el.tagName || '').toLowerCase();
        if (/^(h[1-6]|table|ul|ol|img|svg|canvas|section|article|header|footer|nav|main)$/.test(selfTag)) {
          return el;
        }
        var node = el;
        var maxDepth = 6;
        while (node && node !== document.body && node.id !== 'root' && maxDepth-- > 0) {
          var cls = node.className || '';
          if (typeof cls === 'string' && (
            /rounded-(lg|xl|2xl)/.test(cls) ||
            /shadow/.test(cls) ||
            /\\bp-[4-9]\\b/.test(cls) ||
            /\\bp-1[0-9]/.test(cls) ||
            node.getAttribute('role') ||
            node.hasAttribute('data-section') ||
            node.hasAttribute('data-card')
          )) {
            return node;
          }
          if (node.parentElement && node.parentElement !== document.body && node.parentElement.id !== 'root') {
            node = node.parentElement;
          } else {
            break;
          }
        }
        return el;
      }

      function onHover(e) {
        if (!polishActive) return;
        if (currentHighlight) currentHighlight.classList.remove('__polish-highlight');
        var target = snapToMeaningful(e.target);
        target.classList.add('__polish-highlight');
        currentHighlight = target;
      }
      function onOut(e) {
        if (currentHighlight) currentHighlight.classList.remove('__polish-highlight');
        currentHighlight = null;
      }
      function onClick(e) {
        if (!polishActive) return;
        e.preventDefault();
        e.stopPropagation();
        var target = snapToMeaningful(e.target);
        var rect = target.getBoundingClientRect();
        if (currentHighlight) currentHighlight.classList.remove('__polish-highlight');
        polishActive = false;
        document.body.classList.remove('__polish-active');
        cursorEl.style.display = 'none';
        document.removeEventListener('mousemove', onMouseMove, true);
        window.parent.postMessage({
          type: 'POLISH_ELEMENT_SELECTED',
          element: {
            tag: target.tagName,
            classes: target.className.replace(/__polish-highlight/g, '').trim(),
            text: (target.textContent || '').slice(0, 100).trim(),
            htmlSnippet: target.outerHTML.replace(/ class="[^"]*__polish[^"]*"/g, function(m) { return m.replace(/__polish-highlight/g, '').replace(/\\s+/g, ' '); }).slice(0, 500),
            rect: { top: rect.top, left: rect.left, width: rect.width, height: rect.height }
          }
        }, '*');
      }

      window.addEventListener('message', function(e) {
        if (e.data && e.data.type === 'POLISH_ENTER') {
          polishActive = true;
          document.body.classList.add('__polish-active');
          cursorEl.style.display = 'block';
          document.addEventListener('mousemove', onMouseMove, true);
          document.body.addEventListener('mouseover', onHover, true);
          document.body.addEventListener('mouseout', onOut, true);
          document.body.addEventListener('click', onClick, true);
        } else if (e.data && e.data.type === 'POLISH_EXIT') {
          polishActive = false;
          document.body.classList.remove('__polish-active');
          cursorEl.style.display = 'none';
          document.removeEventListener('mousemove', onMouseMove, true);
          if (currentHighlight) currentHighlight.classList.remove('__polish-highlight');
          currentHighlight = null;
          document.body.removeEventListener('mouseover', onHover, true);
          document.body.removeEventListener('mouseout', onOut, true);
          document.body.removeEventListener('click', onClick, true);
        }
      });
    })();

    // Error reporting: send compile/runtime errors to parent
    window.__artifactErrorSent = false;
    function reportArtifactError(msg) {
      if (window.__artifactErrorSent) return;
      window.__artifactErrorSent = true;
      window.parent.postMessage({
        type: 'ARTIFACT_ERROR',
        payload: { message: msg }
      }, '*');
    }

    // Catch uncaught runtime errors
    window.onerror = function(msg, source, line, col, err) {
      reportArtifactError((err && err.message) || String(msg));
    };
    window.addEventListener('unhandledrejection', function(e) {
      reportArtifactError(e.reason && e.reason.message ? e.reason.message : String(e.reason));
    });

    // Patch ReactDOM.render to wrap with error boundary
    (function() {
      class ArtifactErrorBoundary extends React.Component {
        constructor(props) { super(props); this.state = { hasError: false }; }
        static getDerivedStateFromError() { return { hasError: true }; }
        componentDidCatch(error) { reportArtifactError(error.message || String(error)); }
        render() { return this.state.hasError ? null : this.props.children; }
      }
      var origRender = ReactDOM.render;
      ReactDOM.render = function(element, container) {
        var wrapped = React.createElement(ArtifactErrorBoundary, null, element);
        return origRender.call(ReactDOM, wrapped, container);
      };
    })();
  ${SC}

  ${artifactCode}

  <script>
    // After Babel processes text/babel scripts, check if render succeeded
    // Babel standalone transforms on DOMContentLoaded, so we check shortly after
    window.addEventListener('DOMContentLoaded', function() {
      setTimeout(function() {
        if (!window.__artifactErrorSent) {
          var root = document.getElementById('root');
          if (root && root.children.length > 0) {
            window.parent.postMessage({ type: 'ARTIFACT_READY' }, '*');
          } else if (!window.__artifactErrorSent) {
            reportArtifactError('Dashboard code did not render any content');
          }
        }
      }, 500);
    });
  ${SC}
</body>
</html>`;
});

// Re-send data when it changes
watch([visualizationsData, iframeReady], () => {
  if (iframeReady.value && visualizationsData.value.length > 0) {
    sendDataToIframe();
  }
});
</script>
