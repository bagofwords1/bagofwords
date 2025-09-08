<template>
    <div class="container mx-auto">
        <!-- Header: Kept -->
        <div v-if="props.edit" class="w-full p-2 flex justify-between text-sm sticky top-0 z-50 border-b-2 bg-white">
            <div class="flex items-center gap-2">
                <div class="space-x-0">
                    <UTooltip text="Collapse">
                        <button @click="$emit('toggleSplitScreen')" class="text-xs items-center flex hover:bg-gray-100 px-0 py-1 rounded">
                            <Icon name="heroicons:x-mark" class="w-4 h-4" />
                        </button>
                    </UTooltip>
                </div>
                <div class="font-medium text-gray-700">
                    Dashboard
                </div>
            </div>
            <div class="space-x-2 flex items-center">
                <USelectMenu
                    id="dash-theme"
                    v-model="themeOverride"
                    :options="themeOptions"
                    option-attribute="label"
                    value-attribute="value"
                    size="xs"
                    icon="i-heroicons-paint-brush-20-solid"
                    class="min-w-[140px]"
                    :placeholder="currentThemeDisplay"
                >
                    <template #label>
                        {{ currentThemeDisplay }}
                    </template>
                    <template #option="{ option }">
                        <div class="flex items-center justify-between w-full">
                            <span>{{ option.label }}</span>
                            <Icon v-if="option.selected" name="heroicons:check" class="w-4 h-4 text-green-500" />
                        </div>
                    </template>
                </USelectMenu>
                <UPopover v-if="props.edit" v-model="showAddMenu" :popper="{ placement: 'bottom-start' }">
                    <button class="text-lg items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded">
                        <Icon name="heroicons:plus" />
                    </button>
                    <template #panel>
                        <div class="p-1 min-w-[160px]">
                            <UButton size="xs" color="gray" variant="ghost" icon="i-heroicons-plus-circle" class="w-full justify-start" @click="handleAddMenuSelect('text')">
                                Add Text
                            </UButton>
                        </div>
                    </template>
                </UPopover>
                <UTooltip text="Rerun report">
                    <button @click="rerunReport" class="text-lg items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded">
                        <Icon name="heroicons:play" />
                    </button>
                </UTooltip>
                <CronModal :report="report" />
                <UTooltip text="Open dashboard in a new tab" v-if="report.status === 'published'">
                    <a :href="`/r/${report.id}`" target="_blank" class="text-lg items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded">
                        <Icon name="heroicons:arrow-top-right-on-square" />
                    </a>
                </UTooltip>
                <UTooltip text="Full screen">
                    <button @click="openModal" class="text-lg items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded">
                        <Icon name="heroicons:arrows-pointing-out" />
                    </button>
                </UTooltip>
                <ShareModal :report="report" />
            </div>
        </div>
    
        <!-- Main container for grid and floating editor -->
        <div class="relative w-full h-full dashboard-area bg-white" :style="wrapperStyle">
            <!-- Gridstack Container -->
            <div ref="gridstackContainer"
                 class="grid-stack main-grid"
                 :style="{
                    transform: `scale(${props.edit ? zoom : 1})`,
                    transformOrigin: 'top left'
                 }"
                 @wheel="handleWheel">
    
                <!-- Gridstack Items -->
                <div v-for="widget in allWidgets"
                     :key="widget.id"
                     class="grid-stack-item"
                     :gs-id="widget.id"
                     :gs-x="widget.x"
                     :gs-y="widget.y"
                     :gs-w="widget.width"
                     :gs-h="widget.height"
                     :gs-auto-position="widget.x === undefined || widget.y === undefined"
                     @mouseenter.stop
                     @mouseleave.stop>
    
                    <div :class="['grid-stack-item-content','rounded','overflow-hidden','flex','flex-col','relative','p-0','shadow-sm', { 'border': widget.type !== 'text', 'text-hover': widget.type === 'text' && props.edit }]" :style="[itemStyle, (widget.type==='text' && props.edit) ? { border: '1px solid transparent', '--tw-card-border': tokens.value?.cardBorder || '#e5e7eb' } : {}]">
                        <!-- Controls Overlay -->
                        <div v-if="props.edit" class="absolute right-1 top-1 z-20 flex gap-1 p-1 rounded ">
                            <!-- Regular Widget Remove -->
                            <button v-if="widget.type !== 'text'" title="Remove Widget" class="text-xs items-center flex gap-0.5 hover:bg-red-100 text-red-400 px-1 py-0.5 rounded " @click="removeWidget(widget)">
                                <Icon name="heroicons:trash" class="w-3 h-3"/>
                            </button>
                            <!-- Text Widget Remove (only for EXISTING widgets) -->
                            <button v-if="widget.type === 'text' && !widget.isNew" title="Remove Text" class="text-xs items-center flex gap-0.5 hover:bg-red-100 text-red-400 px-1 py-0.5 rounded " @click="removeTextWidget(widget)">
                                 <Icon name="heroicons:trash" class="w-3 h-3"/>
                            </button>
                            <!-- Toggle Edit / Cancel New -->
                            <button v-if="widget.type === 'text'" :title="widget.isNew ? 'Cancel Adding Text' : 'Edit Text'" class="text-xs items-center flex gap-0.5 hover:bg-blue-100 text-blue-400 px-1 py-0.5 rounded " @click="toggleTextEdit(widget)">
                                <Icon name="heroicons:pencil" v-if="!widget.isEditing && !widget.isNew" class="w-3 h-3"/>
                                <Icon name="heroicons:x-mark" v-if="widget.isEditing || widget.isNew" class="w-3 h-3"/> <!-- Show X for editing or new -->
                            </button>
                        </div>
    
                        <!-- Text Widget Display/Edit -->
                        <template v-if="widget.type === 'text'">
                            <div v-if="widget.isEditing" class="p-1 flex-grow flex flex-col min-h-0">
                                 <TextWidgetEditor :textWidget="widget"
                                    @save="(content) => saveTextWidget(content, widget)"
                                    @cancel="cancelTextEdit(widget)"
                                    class="flex-grow min-h-0"
                                 />
                            </div>
                            <div v-else class="p-2 flex-grow overflow-auto">
                                <component
                                  :is="getCompForType('text_widget')"
                                  :key="`${widget.id}:${themeOverride || report?.report_theme_name || report?.theme_name}`"
                                  :widget="widget"
                                  :step="widget"
                                  :view="widget.view"
                                  :reportThemeName="themeOverride || report?.report_theme_name || report?.theme_name"
                                  :reportOverrides="report?.theme_overrides"
                                />
                            </div>
                        </template>
    
                        <!-- Regular Widget Display -->
                        <template v-else>
                            <div class="flex hidden items-center text-sm py-1 px-2 flex-shrink-0 border-b h-[30px] bg-gray-50 rounded-t">
                                <span class="font-medium truncate text-gray-700">{{ widget.title || 'Widget' }}</span>
                                <span v-if="widget.last_step?.data?.loadingColumn" class="text-gray-400 ml-2 text-xs italic">Loading...</span>
                            </div>
                            <div class="flex-grow overflow-auto p-2 min-h-0">
                                <div v-if="resolvedComp(widget)" class="mt-1 h-full">
                                    <component
                                        :key="`${widget.id}:${themeOverride || report?.report_theme_name || report?.theme_name}`"
                                        v-show="!widget.show_data_model && !widget.show_data"
                                        :is="resolvedComp(widget)"
                                        :widget="widget"
                                        :data="widget.last_step?.data"
                                        :data_model="widget.last_step?.data_model"
                                        :step="widget.last_step"
                                        :view="widget.last_step?.view"
                                        :reportThemeName="themeOverride || report?.report_theme_name || report?.theme_name"
                                        :reportOverrides="report?.theme_overrides"
                                    />
                                    <div v-if="widget.show_data_model" class="text-xs p-1 bg-gray-50 rounded overflow-auto h-full border">
                                        <pre class="text-[10px] whitespace-pre-wrap">{{ widget.last_step?.data_model }}</pre>
                                    </div>
                                    <div v-if="widget.show_data" class="text-xs p-1 bg-gray-50 rounded overflow-auto h-full border">
                                        <pre class="text-[10px] whitespace-pre-wrap">{{ widget.last_step?.data }}</pre>
                                    </div>
                                </div>
                                <div v-else-if="widget.last_step?.type == 'init'" class="text-center items-center flex flex-col justify-center h-full text-gray-500">
                                    <SpinnerComponent />
                                    <span class="mt-2 text-sm">Loading...</span>
                                </div>
                                 <div v-else class="text-center items-center flex flex-col justify-center h-full text-gray-400 italic text-sm">
                                    No data or visualization available.
                                </div>
                            </div>
                        </template>
                    </div>
                </div>
            </div>
    
        </div>
    
        <!-- Fullscreen Modal -->
        <Teleport to="body">
            <UModal v-model="isModalOpen" :ui="{ width: 'sm:max-w-[98vw]', height: 'h-[100vh]' }">
                <div class="h-full flex flex-col">
                    <!-- Modal Header -->
                     <div class="p-2 flex justify-between items-center border-b ">
                        <span class="text-sm font-medium text-gray-700 pl-2">Fullscreen View</span>
                        <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" class="-my-1" @click="closeModal" />
                    </div>
    
                    <!-- Modal Content Area -->
                    <div class="flex-1 overflow-auto p-4" :style="wrapperStyle">
                        <!-- Modal Gridstack Container -->
                        <div ref="modalGridstackContainer"
                             class="grid-stack grid-stack-modal"
                             :style="{ transform: `scale(${modalZoom})`, transformOrigin: 'top left' }">
                            <!-- Modal Grid Items -->
                            <div v-for="widget in allWidgets"
                                 :key="`modal-${widget.id}`"
                                 class="grid-stack-item"
                                 :gs-id="`modal-${widget.id}`"
                                 :gs-x="widget.x"
                                 :gs-y="widget.y"
                                 :gs-w="widget.width"
                                 :gs-h="widget.height">
                                 <div :class="['grid-stack-item-content','rounded','overflow-hidden','flex','flex-col','relative','p-0','shadow-sm', { 'border': widget.type !== 'text' }]" :style="itemStyle">
                                    <template v-if="widget.type === 'text'">
                                        <div class="p-2 flex-grow overflow-auto">
                                            <component
                                              :is="getCompForType('text_widget')"
                                              :key="`modal-${widget.id}:${themeOverride || report?.report_theme_name || report?.theme_name}`"
                                              :widget="widget"
                                              :step="widget"
                                              :view="widget.view"
                                              :reportThemeName="themeOverride || report?.report_theme_name || report?.theme_name"
                                              :reportOverrides="report?.theme_overrides"
                                            />
                                        </div>
                                    </template>
                                    <template v-else>
                                        <div class="flex hidden items-center text-sm py-1 px-2 flex-shrink-0 border-b h-[30px] bg-gray-50 rounded-t">
                                             <span class="font-medium truncate text-gray-700">{{ widget.title || 'Widget' }}</span>
                                             <span v-if="widget.last_step?.data?.loadingColumn" class="text-gray-400 ml-2 text-xs italic">Loading...</span>
                                        </div>
                                        <div class="flex-grow overflow-auto p-2 min-h-0">
                                            <div v-if="resolvedComp(widget)" class="mt-1 h-full">
                                                <component
                                                    :key="`modal-${widget.id}:${themeOverride || report?.report_theme_name || report?.theme_name}`"
                                                    :is="resolvedComp(widget)"
                                                    :widget="widget"
                                                    :data="widget.last_step?.data"
                                                    :data_model="widget.last_step?.data_model"
                                                    :step="widget.last_step"
                                                    :view="widget.last_step?.view"
                                                    :reportThemeName="themeOverride || report?.report_theme_name || report?.theme_name"
                                                    :reportOverrides="report?.theme_overrides"
                                                />
                                            </div>
                                            <div v-else-if="widget.last_step?.type == 'init'" class="text-center items-center flex flex-col justify-center h-full text-gray-500">
                                                 <SpinnerComponent /><span class="mt-2 text-sm">Loading...</span>
                                            </div>
                                            <div v-else class="text-center items-center flex flex-col justify-center h-full text-gray-400 italic text-sm">
                                                No data or visualization available.
                                            </div>
                                        </div>
                                    </template>
                                </div>
                            </div>
                        </div>
                    </div>

                </div>
            </UModal>
        </Teleport>
    
    </div>
    </template>
    
    <script setup lang="ts">
    // Import Gridstack CSS FIRST
    import 'gridstack/dist/gridstack.min.css';
    import { GridStack } from 'gridstack';
    import { ref, computed, onMounted, onBeforeUnmount, nextTick, watch, defineAsyncComponent } from 'vue';
    import { useMyFetch } from '~/composables/useMyFetch';
    import RenderVisual from './RenderVisual.vue';
    import TextWidgetEditor from './TextWidgetEditor.vue';
    import RenderCount from './RenderCount.vue';
    import SpinnerComponent from './SpinnerComponent.vue';
    import CronModal from './CronModal.vue';
    import ShareModal from './ShareModal.vue';
    import AgGridComponent from './AgGridComponent.vue';
    import { resolveEntryByType } from '@/components/dashboard/registry'
    import { themes } from '@/components/dashboard/themes'
    import { useDashboardTheme } from '@/components/dashboard/composables/useDashboardTheme'

    const toast = useToast();
    const emit = defineEmits(['removeWidget', 'toggleSplitScreen']);

    const props = defineProps<{
        report: any
        edit: boolean
        widgets: any[]
        textWidgetsIds?: string[]
    }>();

    const reportThemeName = ref(props.report?.report_theme_name || 'default');

    // --- Refs ---
    const gridstackContainer = ref<HTMLElement | null>(null);
    const grid = ref<GridStack | null>(null);
    const textWidgets = ref<any[]>([]);
    const displayedWidgets = ref<any[]>([]);

    // Zoom state
    const zoom = ref(1);
    const zoomStep = 0.1;
    const minZoom = 0.5;
    const maxZoom = 1.5;

    // Fullscreen Modal state
    const isModalOpen = ref(false);
    const modalGridstackContainer = ref<HTMLElement | null>(null);
    const modalGrid = ref<GridStack | null>(null);
    const modalZoom = ref(1);

    // --- Gridstack Configuration ---
    const GRID_CELL_HEIGHT = 40;
    const GRID_MARGIN = 10;
    const GRID_COLS = 12;

    // --- Theme tokens for container ---
    const themeOverride = ref<string>('');
    const themeNames = Object.keys(themes || {});
    
    // Current effective theme name (what's actually being used)
    const themeNameRef = computed(() => themeOverride.value || props.report?.report_theme_name || props.report?.theme_name || 'default')
    
    // Current displayed theme in dropdown (what user sees selected)
    const currentThemeDisplay = computed(() => {
        if (themeOverride.value) {
            return themeOverride.value;
        }
        const reportTheme = props.report?.report_theme_name || props.report?.theme_name;
        return reportTheme || 'default';
    });
    
    // Options for the theme dropdown
    const themeOptions = computed(() => {
        const currentTheme = currentThemeDisplay.value;
        const options = [];
        
        // Add report's default theme option
        const reportTheme = props.report?.report_theme_name || props.report?.theme_name || 'default';
        options.push({ 
            label: `${reportTheme} (report)`, 
            value: '',
            selected: !themeOverride.value
        });
        
        // Add other theme options
        themeNames.forEach(themeName => {
            options.push({
                label: themeName,
                value: themeName,
                selected: themeOverride.value === themeName
            });
        });
        
        return options;
    })
    const reportOverridesRef = computed(() => props.report?.theme_overrides || {})
    const { tokens } = useDashboardTheme(themeNameRef, reportOverridesRef, null)
    const wrapperStyle = computed(() => ({ backgroundColor: tokens.value?.background || '', color: tokens.value?.textColor || '' }))
    const itemStyle = computed(() => ({
        backgroundColor: tokens.value?.cardBackground || tokens.value?.background || '',
        color: tokens.value?.textColor || '',
        borderColor: tokens.value?.cardBorder || ''
    }))
    const headerStyle = computed(() => ({
        backgroundColor: tokens.value?.cardBackground || tokens.value?.background || '',
        color: tokens.value?.textColor || '',
        borderColor: tokens.value?.cardBorder || '#e5e7eb'
    }))

    // --- Computed ---
    const allWidgets = computed(() => {
        const regular = displayedWidgets.value.map(w => ({
            ...w,
            type: 'regular',
            showControls: w.showControls ?? false,
            show_data: w.show_data ?? false,
            show_data_model: w.show_data_model ?? false
        }));
        const text = textWidgets.value.map(w => ({
            ...w,
            type: 'text',
            isEditing: w.isEditing ?? false,
            showControls: w.showControls ?? false,
            isNew: w.isNew ?? false
        }));
        return [...regular, ...text].sort((a, b) => (a.y ?? 0) - (b.y ?? 0) || (a.x ?? 0) - (b.x ?? 0));
    });

    // --- Lifecycle Hooks ---
    onMounted(async () => {
        initializeMainGrid();
        await fetchAllWidgets();
        loadWidgetsIntoGrid(grid.value, allWidgets.value);
        document.addEventListener('keydown', handleEscKey);
    });

    onBeforeUnmount(() => {
        grid.value?.destroy(false);
        modalGrid.value?.destroy(false);
        document.removeEventListener('keydown', handleEscKey);
    });

    // --- Grid Initialization ---
    function initializeMainGrid() {
        if (gridstackContainer.value && !grid.value) {
            grid.value = GridStack.init({
                column: GRID_COLS,
                cellHeight: GRID_CELL_HEIGHT,
                margin: GRID_MARGIN,
                float: true,
                sizeToContent: false,
                disableDrag: !props.edit,
                disableResize: !props.edit,
            }, gridstackContainer.value);

            grid.value.on('change', handleGridChange);
            grid.value.on('added', handleGridAdded);
            grid.value.on('removed', handleGridRemoved);
        }
    }

    async function initializeModalGrid() {
        await nextTick();
        if (modalGridstackContainer.value && !modalGrid.value) {
            modalGrid.value = GridStack.init({
                column: GRID_COLS,
                cellHeight: GRID_CELL_HEIGHT,
                margin: GRID_MARGIN,
                float: true,
                staticGrid: true,
            }, modalGridstackContainer.value);
            // Clear any nodes carried over by GridStack DOM (safety)
            const nodes = [...(modalGrid.value.engine.nodes || [])];
            nodes.forEach(n => n?.el && modalGrid.value?.removeWidget(n.el as HTMLElement, false, false));
            // Add widgets with absolute positions (no autoPosition)
            await nextTick();
            for (const widget of allWidgets.value) {
                const id = `modal-${widget.id}`;
                const el = document.querySelector(`[gs-id="${id}"]`);
                if (el) {
                    modalGrid.value.addWidget(el as HTMLElement, { id, x: widget.x, y: widget.y, w: widget.width, h: widget.height, autoPosition: false });
                }
            }
        } else if (modalGrid.value) {
             loadWidgetsIntoGrid(modalGrid.value, allWidgets.value, true);
        }
    }

    // --- Data Fetching & Loading ---
    async function fetchAllWidgets() {
        await Promise.all([
            getTextWidgetsInternal(),
            updateDisplayedWidgets(props.widgets || [])
        ]);
    }

    async function getTextWidgetsInternal() {
        try {
            const url = props.edit ? `/api/reports/${props.report.id}/text_widgets` : `/api/r/${props.report.id}/text_widgets`;
            const { data, error } = await useMyFetch(url, { method: 'GET' });

            if (error.value) throw error.value;

            const widgetsArray = Array.isArray(data.value) ? data.value : [];
            const currentTextWidgetsMap = new Map(textWidgets.value.map(w => [w.id, { isEditing: w.isEditing, showControls: w.showControls }]));

            textWidgets.value = widgetsArray.map((widgetDataFromApi: any) => ({
                ...widgetDataFromApi,
                type: 'text',
                isEditing: currentTextWidgetsMap.get(widgetDataFromApi.id)?.isEditing ?? false,
                showControls: currentTextWidgetsMap.get(widgetDataFromApi.id)?.showControls ?? false,
                x: widgetDataFromApi.x ?? 0,
                y: widgetDataFromApi.y ?? 0,
                width: widgetDataFromApi.width ?? 4,
                height: widgetDataFromApi.height ?? 5
            }));
        } catch (error: any) {
            console.error('Failed to fetch text widgets:', error);
            toast.add({ title: 'Error', description: `Failed to load text widgets: ${error.message || 'Unknown error'}`, color: 'red' });
        }
    }

    function updateDisplayedWidgets(newWidgets: any[]) {
        const currentWidgetsMap = new Map(displayedWidgets.value.map(w => [w.id, {
            show_data: w.show_data,
            show_data_model: w.show_data_model,
            showControls: w.showControls
        }]));
        displayedWidgets.value = (newWidgets || []).map(newWidget => ({
            ...newWidget,
            show_data: currentWidgetsMap.get(newWidget.id)?.show_data ?? false,
            show_data_model: currentWidgetsMap.get(newWidget.id)?.show_data_model ?? false,
            showControls: currentWidgetsMap.get(newWidget.id)?.showControls ?? false,
            x: newWidget.x ?? 0,
            y: newWidget.y ?? 0,
            width: newWidget.width ?? 6,
            height: newWidget.height ?? 7
        }));
    }

    // Generic function to load widgets into a Gridstack instance
    async function loadWidgetsIntoGrid(targetGrid: GridStack | null, widgetsToLoad: any[], useModalIds = false) {
        if (!targetGrid) return;

        await nextTick();
        targetGrid.batchUpdate();

        const currentGridItems = new Map(targetGrid.engine.nodes.map(n => [n.id, n]));
        const widgetsMap = new Map(widgetsToLoad.map(w => [w.id, w]));

        // Remove items from grid no longer in data
         currentGridItems.forEach(node => {
            const widgetId = useModalIds && typeof node.id === 'string' && node.id.startsWith('modal-') ? node.id.substring(6) : node.id;
            if (!widgetsMap.has(widgetId)) {
                 if (node.el) targetGrid.removeWidget(node.el, false, false);
            }
        });

        // Add/Update widgets
        for (const widget of widgetsToLoad) {
            const gridItemId = useModalIds ? `modal-${widget.id}` : widget.id;
            const existingNode = currentGridItems.get(gridItemId);
            const element = document.querySelector(`[gs-id="${gridItemId}"]`);

            if (element) {
                const gsOptions = {
                    x: widget.x,
                    y: widget.y,
                    w: widget.width,
                    h: widget.height,
                    id: gridItemId,
                    autoPosition: false
                };

                if (existingNode) {
                    if (existingNode.x !== gsOptions.x || existingNode.y !== gsOptions.y || existingNode.w !== gsOptions.w || existingNode.h !== gsOptions.h) {
                        targetGrid.update(element as HTMLElement, gsOptions);
                    }
                } else {
                    targetGrid.addWidget(element as HTMLElement, gsOptions as any);
                }
            } else {
                // Element might not be rendered yet if just added, `addWidget` handles this case later
                // Or warn if it's an existing widget that's missing
                if (!widget.isNew) { // Avoid warning for newly added ones before addWidget runs
                     console.warn(`Element for existing widget ID ${gridItemId} not found in DOM during load.`);
                }
            }
        }

        targetGrid.commit();
    }

    // --- Watchers ---
    watch(() => props.edit, (newEditMode) => {
        if (grid.value) {
            if (newEditMode) {
                grid.value.enable();
            } else {
                grid.value.disable();
                allWidgets.value.forEach(w => w.showControls = false);
            }
        }
    });

    watch(() => props.widgets, async (newWidgets) => {
        updateDisplayedWidgets(newWidgets || []);
        await loadWidgetsIntoGrid(grid.value, allWidgets.value);
    }, { deep: true, immediate: true });

    // Watch for immediate theme application 
    watch(tokens, (newTokens) => {
        if (newTokens && gridstackContainer.value) {
            // Force immediate style application to grid container
            nextTick(() => {
                if (gridstackContainer.value) {
                    const style = `background-color: ${newTokens.background || '#ffffff'}; color: ${newTokens.textColor || '#0f172a'};`;
                    gridstackContainer.value.parentElement!.setAttribute('style', style);
                }
            });
        }
    }, { immediate: true });

    watch(themeOverride, async (val, oldVal) => {
        if (val === oldVal) return;
        if (!props.report?.id) return;
        // If empty value is chosen, skip persisting for now
        if (val === undefined || val === null || val === '') return;
        try {
            const { error } = await useMyFetch(`/api/reports/${props.report.id}`, {
                method: 'PUT',
                body: { theme_name: val }
            });
            if (error.value) throw error.value;
            // Update local report object so UI is in sync
            if (props.report) {
                (props.report as any).theme_name = val;
                (props.report as any).report_theme_name = val;
            }
        } catch (e: any) {
            console.error('Failed to update report theme', e);
            toast.add({ title: 'Failed to save theme', description: e?.message || String(e), color: 'red' });
        }
    });
 
    watch(allWidgets, async (currentWidgets, oldWidgets) => {
        // Simplified condition: Reload if length changes or if it's a deep change (heuristically)
        if (currentWidgets.length !== oldWidgets?.length || JSON.stringify(currentWidgets) !== JSON.stringify(oldWidgets)) {
            await loadWidgetsIntoGrid(grid.value, currentWidgets);
            if (isModalOpen.value && modalGrid.value) {
                await loadWidgetsIntoGrid(modalGrid.value, currentWidgets, true);
            }
        }
    }, { deep: true });


    // --- Gridstack Event Handlers ---
    const handleGridChange = (event: Event, items: any[]) => {
        if (!props.edit) return;
        items.forEach(item => {
            const widget = findWidgetById(item.id);
            if (!widget) {
                 console.warn(`Widget with ID ${item.id} not found in local data during grid change.`);
                 return;
            }
            const newX = item.x;
            const newY = item.y;
            const newWidth = item.w;
            const newHeight = item.h;

            if (widget.x !== newX || widget.y !== newY || widget.width !== newWidth || widget.height !== newHeight) {
                widget.x = newX;
                widget.y = newY;
                widget.width = newWidth;
                widget.height = newHeight;
                if (!widget.isNew) {
                    updateWidgetBackend(widget); // Consider debouncing this
                }
            }
        });
    };

    const handleGridAdded = (event: Event, items: any[]) => {
        // Usually triggered by makeWidget or addWidget. Log if needed for debugging.
        // console.log(`Grid added event: ${items.map(i=>i.id).join(', ')}`);
    };

    const handleGridRemoved = (event: Event, items: any[]) => {
        // Sync local data state AFTER gridstack removes the element
        items.forEach(item => {
            const widgetId = item.id;
            const textIndex = textWidgets.value.findIndex(w => w.id === widgetId);
            if (textIndex !== -1) {
                textWidgets.value.splice(textIndex, 1);
            } else {
                const regularIndex = displayedWidgets.value.findIndex(w => w.id === widgetId);
                if (regularIndex !== -1) {
                    displayedWidgets.value.splice(regularIndex, 1);
                     emit('removeWidget', { id: widgetId });
                }
            }
        });
    };

    // --- Widget Find & Update ---
    const findWidgetById = (id: string): any | undefined => {
        const cleanId = id?.startsWith('modal-') ? id.substring(6) : id;
        return allWidgets.value.find(w => w.id === cleanId);
    };

    async function updateWidgetBackend(widget: any) {
        if (!widget || !widget.id) return;
        const isText = widget.type === 'text';
        const url = isText
            ? `/api/reports/${props.report.id}/text_widgets/${widget.id}`
            : `/api/reports/${props.report.id}/widgets/${widget.id}`;

        const requestBody = {
            id: widget.id,
            x: widget.x,
            y: widget.y,
            width: widget.width,
            height: widget.height,
            ...(isText && widget.content && { content: widget.content })
        };
        try {
            const { error } = await useMyFetch(url, { method: 'PUT', body: requestBody });
            if (error.value) throw error.value;
        } catch (error: any) {
            console.error(`Failed to update widget ${widget.id}`, error);
            toast.add({ title: 'Error Saving', description: `Could not save changes for widget. ${error.message || ''}`, color: 'red' });
        }
    }

    // --- Widget CRUD ---
    async function removeWidget(widget: any) {
        try {
            const { error } = await useMyFetch(`/api/reports/${props.report.id}/widgets/${widget.id}`, { method: 'DELETE' });
            if (error.value) throw error.value;

            const el = grid.value?.engine.nodes.find(n => n.id === widget.id)?.el;
            if (el && grid.value) {
                 grid.value.removeWidget(el); // Triggers 'removed' event
            } else {
                 console.warn(`Could not find element for widget ${widget.id} in grid to remove.`);
                const index = displayedWidgets.value.findIndex(w => w.id === widget.id);
                if (index !== -1) displayedWidgets.value.splice(index, 1);
                 emit('removeWidget', { id: widget.id });
            }
            toast.add({ title: 'Widget Removed' });
        } catch (error: any) {
            console.error(`Failed to remove widget ${widget.id}`, error);
            toast.add({ title: 'Error', description: `Failed to remove widget. ${error.message || ''}`, color: 'red' });
        }
    }
    async function removeTextWidget(widget: any) {
        try {
            if (!widget.isNew) {
                const { error } = await useMyFetch(`/api/reports/${props.report.id}/text_widgets/${widget.id}`, { method: 'DELETE' });
                if (error.value) throw error.value;
            }

            const el = grid.value?.engine.nodes.find(n => n.id === widget.id)?.el;
            if (el && grid.value) {
                grid.value.removeWidget(el); // Triggers 'removed' event
            } else {
                const index = textWidgets.value.findIndex(w => w.id === widget.id);
                if (index !== -1) textWidgets.value.splice(index, 1);
                else {
                     console.warn(`Element/Data for text widget ${widget.id} not found for removal.`);
                }
            }

            if (!widget.isNew) {
                toast.add({ title: 'Text Widget Removed' });
            }
        } catch (error: any) {
            console.error(`Failed to remove text widget ${widget.id}`, error);
            toast.add({ title: 'Error', description: `Failed to remove text widget. ${error.message || ''}`, color: 'red' });
        }
    }

    // --- Text Widget Specific ---
    const addNewTextWidgetToGrid = async () => {
        if (!grid.value) {
            toast.add({ title: 'Error', description: 'Grid is not initialized.', color: 'red' });
            return;
        }

        const tempId = `new-${Date.now()}`;
        const newWidget = {
            id: tempId,
            content: '<p>Start typing...</p>',
            x: 0, y: 0, width: 4, height: 5,
            type: 'text', isEditing: true, isNew: true, showControls: true
        };

        textWidgets.value.push(newWidget);

        await nextTick();

        const element = document.querySelector(`[gs-id="${tempId}"]`);
        if (element && grid.value) {
            grid.value.makeWidget(element as HTMLElement);
            // Check if Gridstack picked up the attributes correctly, update if not
            const node = grid.value.engine.nodes.find(n => n.id === tempId);
            if (node && (node.x !== newWidget.x || node.y !== newWidget.y || node.w !== newWidget.width || node.h !== newWidget.height)) {
                 grid.value.update(element as HTMLElement, { x: newWidget.x, y: newWidget.y, w: newWidget.width, h: newWidget.height });
            }
        } else {
            console.warn(`Could not find DOM element for new widget ${tempId} immediately after adding.`);
            // await loadWidgetsIntoGrid(grid.value, allWidgets.value); // Fallback - might be too slow
        }
    };

    const saveTextWidget = async (content: string, widget: any) => {
        if (!content || content === '<p></p>') {
            toast.add({ title: 'Cannot save', description: 'Text widget content is empty.', color: 'orange' });
            return;
        }

        const widgetIndex = textWidgets.value.findIndex(w => w.id === widget.id);
        if (widgetIndex === -1) {
            console.error(`Cannot find widget ${widget.id} to save.`);
            toast.add({ title: 'Error', description: 'Could not find widget data to save.', color: 'red' });
            return;
        }

        let finalX = widget.x;
        let finalY = widget.y;
        let finalW = widget.width;
        let finalH = widget.height;

        const node = grid.value?.engine.nodes.find(n => n.id === widget.id);
        if (node) {
            finalX = node.x; finalY = node.y; finalW = node.w; finalH = node.h;
        } else {
             console.warn(`Could not find grid node for ${widget.id} when saving. Using stored values.`);
        }

        if (widget.isNew) {
            try {
                const tempNode = grid.value?.engine.nodes.find(n => n.id === widget.id);
                const tempElement = tempNode?.el;

                const { data: newWidgetData, error } = await useMyFetch(`/api/reports/${props.report.id}/text_widgets`, {
                    method: 'POST',
                    body: { content, x: finalX, y: finalY, width: finalW, height: finalH }
                });
                if (error.value) throw error.value;

                if (newWidgetData.value) {
                    const savedWidget = {
                        ...newWidgetData.value,
                        type: 'text',
                        isEditing: false,
                        isNew: false,
                        showControls: true // Ensure controls are ready
                    };

                    if (tempElement && grid.value) {
                        grid.value.removeWidget(tempElement, false, false);
                    } else {
                         console.warn(`Could not find temporary element ${widget.id} to remove.`);
                    }

                    textWidgets.value.splice(widgetIndex, 1, savedWidget);

                    await nextTick();

                    const newElement = document.querySelector(`[gs-id="${savedWidget.id}"]`);

                    if (newElement && grid.value) {
                         grid.value.addWidget(newElement as HTMLElement, {
                             id: savedWidget.id,
                             x: savedWidget.x, y: savedWidget.y,
                             w: savedWidget.width, h: savedWidget.height,
                             autoPosition: false
                         });
                    } else {
                         console.warn(`Could not find new element ${savedWidget.id} in DOM to add to gridstack.`);
                         // Consider fallback: await loadWidgetsIntoGrid(grid.value, allWidgets.value);
                    }

                    toast.add({ title: 'Text Widget Added' });
                 } else { throw new Error("No data returned for new text widget"); }
            } catch (error: any) {
                console.error('Failed to save new text widget', error);
                toast.add({ title: 'Error', description: `Failed to save new text widget. ${error.message || ''}`, color: 'red' });
            }
        } else {
            // Saving edits to an EXISTING widget
            const existingWidget = textWidgets.value[widgetIndex];
            existingWidget.content = content;
            existingWidget.x = finalX;
            existingWidget.y = finalY;
            existingWidget.width = finalW;
            existingWidget.height = finalH;
            existingWidget.isEditing = false;

            await updateWidgetBackend(existingWidget);
        }
    };

    const toggleTextEdit = (widget: any) => {
        if (widget.isNew) {
            removeTextWidget(widget);
        } else {
            const originalWidgetIndex = textWidgets.value.findIndex(w => w.id === widget.id);
            if (originalWidgetIndex !== -1) {
                const originalWidget = textWidgets.value[originalWidgetIndex];
                originalWidget.isEditing = !originalWidget.isEditing;
            } else {
                console.warn(`Could not find original text widget with ID ${widget.id} to toggle edit state.`);
            }
        }
    };

    const cancelTextEdit = (widget: any) => {
        if (widget.isNew) {
             removeTextWidget(widget);
        } else {
            // Find original and toggle editing off
            const originalWidgetIndex = textWidgets.value.findIndex(w => w.id === widget.id);
             if (originalWidgetIndex !== -1) {
                textWidgets.value[originalWidgetIndex].isEditing = false;
             }
        }
    };

    // --- Zoom ---
    const zoomIn = () => { zoom.value = Math.min(zoom.value + zoomStep, maxZoom) };
    const zoomOut = () => { zoom.value = Math.max(zoom.value - zoomStep, minZoom) };
    const resetZoom = () => { zoom.value = 1 };
    const handleWheel = (event: WheelEvent) => {
        if (props.edit && event.ctrlKey) {
            event.preventDefault();
            if (event.deltaY < 0) zoomIn(); else zoomOut();
        }
    };

    // --- Fullscreen Modal ---
    const openModal = async () => {
        isModalOpen.value = true;
        await initializeModalGrid();
    };
    const closeModal = () => {
        isModalOpen.value = false;
        modalZoom.value = 1;
        // Explicitly destroy the modal grid instance and reset the ref
        if (modalGrid.value) {
            modalGrid.value.destroy(false); // false = don't remove DOM elements
            modalGrid.value = null;
        }
    };
    const handleEscKey = (e: KeyboardEvent) => {
        if (e.key === 'Escape' && isModalOpen.value) closeModal();
    };
    const modalZoomIn = () => { modalZoom.value = Math.min(modalZoom.value + zoomStep, maxZoom * 1.2) };
    const modalZoomOut = () => { modalZoom.value = Math.max(modalZoom.value - zoomStep, minZoom) };

    // --- Data/Model Toggles ---
    const toggleDataModel = (widget: any) => {
        widget.show_data_model = !widget.show_data_model;
        if (widget.show_data_model) widget.show_data = false;
    };
    const toggleData = (widget: any) => {
        widget.show_data = !widget.show_data;
        if (widget.show_data) widget.show_data_model = false;
    };

    // --- Other ---
    async function rerunReport() {
         try {
            const { data, error } = await useMyFetch(`/api/reports/${props.report.id}/rerun`, { method: 'POST' });
            if (error.value) throw error.value;
            if (data.value) {
                toast.add({ title: 'Rerunning report', description: data.value.message || 'Report rerun initiated.' });
                // Optionally fetch widgets after delay: setTimeout(fetchAllWidgets, 5000);
            } else {
                 toast.add({ title: 'Note', description: 'Report rerun request sent, but no message received.', color: 'orange' });
            }
        } catch (error: any) {
            console.error('Failed to rerun report:', error);
            toast.add({ title: 'Error', description: `Failed to rerun report. ${error.message || ''}`, color: 'red' });
        }
    }
    const chartVisualTypes = new Set([
        'pie_chart', 'line_chart', 'bar_chart', 'area_chart', 'scatter_plot',
        'heatmap', 'map', 'candlestick', 'treemap', 'radar_chart'
    ]);

    // Frontend-only theme override

    // --- Dashboard component resolution via registry ---
    const compCache = new Map<string, any>();
    function getCompForType(type?: string | null) {
        const t = (type || '').toLowerCase();
        if (!t) return null;
        if (compCache.has(t)) return compCache.get(t);
        const entry = resolveEntryByType(t);
        if (!entry) return null;
        const comp = defineAsyncComponent(entry.load);
        compCache.set(t, comp);
        return comp;
    }
    function resolvedComp(widget: any) {
        return getCompForType(widget?.last_step?.data_model?.type);
    }

    // --- Exposed Methods ---
    async function refreshTextWidgets() {
        console.log("DashboardComponent: Refreshing text widgets...");
        await getTextWidgetsInternal();
        // After fetching, ensure the grid reflects the latest state of allWidgets
        await loadWidgetsIntoGrid(grid.value, allWidgets.value);
        if (isModalOpen.value && modalGrid.value) {
            await loadWidgetsIntoGrid(modalGrid.value, allWidgets.value, true);
        }
    }

    defineExpose({
        refreshTextWidgets
    });

    // --- Add widget menu ---
    const showAddMenu = ref(false)
    const addMenuOptions = [
        { label: 'Add Text', value: 'text' },
    ]
    const addMenuValue = ref<string | null>(null)
    function handleAddMenuSelect(val: string) {
        if (val === 'text') {
            addNewTextWidgetToGrid()
        }
        showAddMenu.value = false
        addMenuValue.value = null
    }

    </script>
    
    <style> /* Use non-scoped style for gridstack overrides if necessary */
    /* Gridstack base styles */
    /* @import 'gridstack/dist/gridstack.min.css'; /* Loaded via JS import */
    
    .grid-stack {
      /* background: #fafafa; */ /* REMOVED: Let parent control background */
      /* Use min-height or let gridstack determine height */
       min-height: 600px; /* Ensure it has some height */
    }
    
    /* Default item content style */
    .grid-stack-item-content {
      background-color: #ffffff;
      color: #2c3e50;
      text-align: left;
      overflow: hidden !important; /* CRITICAL: Prevent content spillover */
      /* inset: 0px; */ /* No default inset, use padding on inner elements */
      position: absolute; /* Needed for Gridstack sizing */
      top: 0; bottom: 0; left: 0; right: 0; /* Fill the item */
    }
    
    /* Improve placeholder appearance */
    .grid-stack-placeholder > .placeholder-content {
      border: 2px dashed #ccc !important;
      background-color: rgba(220, 220, 220, 0.3) !important;
    }
    
    /* Style for the floating text editor */
    .vue-draggable-resizable {
        /* Optional: Add specific styles */
    }
    .vdr.active:before { /* Style when active */
        outline: 2px dashed #42b983;
    }
    

    /* Modal Specific Grid */
    .grid-stack-modal {
        /* background: #f0f0f0; */ /* REMOVED: Let parent control background */
        min-height: 600px; /* Ensure modal grid has some initial size */
        transition: transform 0.2s ease-out; /* Smooth modal zoom */
    }
    
    /* Ensure TextWidgetEditor takes available space */
    .grid-stack-item-content .flex-grow.min-h-0,
    .vue-draggable-resizable .flex-grow.min-h-0 {
        display: flex;
        flex-direction: column;
        overflow: hidden; /* Crucial for editor scroll */
    }
    
    .grid-stack-item-content .flex-grow.min-h-0 > .flex-grow.min-h-0, /* Target the TextWidgetEditor container */
    .vue-draggable-resizable .flex-grow.min-h-0 > .flex-grow.min-h-0 {
        flex-grow: 1;
        display: flex;
        flex-direction: column;
        overflow: hidden; /* Let editor handle internal scroll */
    }
    
    /* Main dashboard area - allow scroll if grid overflows */
    .dashboard-area {
        overflow: auto; /* Or overflow: hidden if grid should not scroll page */
    }
    
    /* Main Grid - apply zoom */
    .main-grid {
         transition: transform 0.2s ease-out;
    }
    /* Hover outline for text widgets in edit mode */
    .text-hover:hover {
        border-color: var(--tw-card-border);
    }
    
    </style>