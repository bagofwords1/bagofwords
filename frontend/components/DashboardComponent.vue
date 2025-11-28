<template>
    <div class="container mx-auto">
        <!-- Header: Kept -->
        <Toolbar
            v-if="props.edit"
            :report="report"
            :edit="props.edit"
            v-model:themeOverride="themeOverride"
            :themeOptions="themeOptions"
            :currentThemeDisplay="currentThemeDisplay"
            @add:text="addNewTextWidgetToGrid"
            @rerun="rerunReport"
            @openFullscreen="openModal"
            @toggleSplitScreen="$emit('toggleSplitScreen')"
        />
    
        <!-- Main container for grid and floating editor -->
        <div class="relative w-full h-full dashboard-area bg-white" :style="wrapperStyle">
            <!-- Loading overlay during initial fetch -->
            <div v-if="isLoading" class="absolute inset-0 z-10 flex items-center justify-center bg-white/60">
                <Spinner class="mr-2 w-4 h-4" />
                <span class="text-sm text-gray-600">Loading dashboard…</span>
            </div>
            <!-- Gridstack Container -->
            <div ref="gridstackContainer"
                 class="grid-stack main-grid"
                 :style="{
                    transform: `scale(${props.edit ? zoom : 1})`,
                    transformOrigin: 'top left'
                 }"
                 @wheel="handleWheel"
            >
    
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
    
                    <WidgetFrame
                        :widget="widget"
                        :edit="props.edit"
                        :isText="widget.type === 'text'"
                        :itemStyle="itemStyle"
                        :cardBorder="tokens.value?.cardBorder || '#e5e7eb'"
                    >
                        <WidgetControls
                            :edit="props.edit"
                            :isText="widget.type === 'text'"
                            :isVisualization="widget.isVisualization || false"
                            :queryId="widget.query_id"
                            :widget="widget"
                            :isEditing="widget.isEditing"
                            :isNew="widget.isNew"
                            @remove="removeWidget(widget)"
                            @removeText="removeTextWidget(widget)"
                            @toggleTextEdit="toggleTextEdit(widget)"
                            @editVisualization="handleEditVisualization"
                        />

                        <!-- Text widget (legacy with DB reference) -->
                        <template v-if="widget.type === 'text' && widget.id && !widget.content">
                            <TextWidgetView
                                :widget="widget"
                                :themeName="themeOverride || report?.report_theme_name || report?.theme_name"
                                :reportOverrides="report?.theme_overrides"
                                @save="(content) => saveTextWidget(content, widget)"
                                @cancel="cancelTextEdit(widget)"
                            />
                        </template>
                        <!-- Inline text block (AI generated, no DB reference) -->
                        <template v-else-if="widget.type === 'text' && widget.content">
                            <TextBlock
                                :block="widget"
                                :themeName="themeOverride || report?.report_theme_name || report?.theme_name"
                                :reportOverrides="report?.theme_overrides"
                                @save="(content) => saveInlineTextBlock(widget, content)"
                                @cancel="cancelTextEdit(widget)"
                            />
                        </template>
                        <!-- Card block with children -->
                        <template v-else-if="widget.type === 'card'">
                            <CardBlock
                                :block="widget"
                                :themeName="themeOverride || report?.report_theme_name || report?.theme_name"
                                :reportOverrides="report?.theme_overrides"
                                :contentIsMetricCard="cardContainsMetricCard(widget)"
                            >
                                <BlockRenderer
                                    v-for="(child, idx) in widget.children || []"
                                    :key="`card-child-${idx}-${child.visualization_id || child.content?.substring(0,10) || idx}`"
                                    :block="child"
                                    :widget="getWidgetDataForBlock(child)"
                                    :themeName="themeOverride || report?.report_theme_name || report?.theme_name"
                                    :reportOverrides="report?.theme_overrides"
                                    :getWidgetForBlock="getWidgetDataForBlock"
                                />
                            </CardBlock>
                        </template>
                        <!-- Column layout block -->
                        <template v-else-if="widget.type === 'column_layout'">
                            <ColumnLayoutBlock :block="widget">
                                <template v-for="(col, colIdx) in widget.columns || []" :key="colIdx" #[`column-${colIdx}`]>
                                    <div class="flex flex-col gap-4 h-full">
                                        <BlockRenderer
                                            v-for="(child, childIdx) in col.children || []"
                                            :key="`col-${colIdx}-child-${childIdx}`"
                                            :block="child"
                                            :widget="getWidgetDataForBlock(child)"
                                            :themeName="themeOverride || report?.report_theme_name || report?.theme_name"
                                            :reportOverrides="report?.theme_overrides"
                                            :getWidgetForBlock="getWidgetDataForBlock"
                                            class="flex-shrink-0"
                                            :style="{ height: `${(child.height || 6) * 40}px` }"
                                        />
                                    </div>
                                </template>
                            </ColumnLayoutBlock>
                        </template>
                        <!-- Regular visualization -->
                        <template v-else>
                            <RegularWidgetView
                                :widget="widget"
                                :themeName="themeOverride || report?.report_theme_name || report?.theme_name"
                                :reportOverrides="report?.theme_overrides"
                            />
                        </template>
                    </WidgetFrame>
                </div>
            </div>
    
            <!-- Minimal empty state when there are no components -->
            <div v-if="allWidgets.length === 0 && !isLoading" class="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
                <Icon name="heroicons-chart-bar" class="w-6 h-6 text-gray-400 block mb-2" />
                <div v-if="props.edit" class="text-gray-400 text-sm">Write a prompt to create a dashboard</div>
                <div v-else class="text-gray-400 text-sm">No dashboard items yet</div>
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
                        <FullscreenGrid
                          :widgets="allWidgets"
                          :report="report"
                          :themeName="themeOverride || report?.report_theme_name || report?.theme_name"
                          :reportOverrides="report?.theme_overrides"
                          :tokens="tokens.value"
                          :itemStyle="itemStyle"
                          :zoom="modalZoom"
                        />
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
    import Toolbar from '@/components/dashboard/Toolbar.vue';
    import WidgetFrame from '@/components/dashboard/WidgetFrame.vue';
    import WidgetControls from '@/components/dashboard/WidgetControls.vue';
    import TextWidgetView from '@/components/dashboard/text/TextWidgetView.vue';
    import RegularWidgetView from '@/components/dashboard/regular/RegularWidgetView.vue';
    import FullscreenGrid from '@/components/dashboard/FullscreenGrid.vue';
import Spinner from '@/components/Spinner.vue';
import BlockRenderer from '@/components/dashboard/blocks/BlockRenderer.vue';
import TextBlock from '@/components/dashboard/blocks/TextBlock.vue';
import CardBlock from '@/components/dashboard/blocks/CardBlock.vue';
import ColumnLayoutBlock from '@/components/dashboard/blocks/ColumnLayoutBlock.vue';
import { resolveEntryByType } from '@/components/dashboard/registry'
import { themes } from '@/components/dashboard/themes'
    import { useDashboardTheme } from '@/components/dashboard/composables/useDashboardTheme'

    const toast = useToast();
    const instanceId = `${Date.now()}-${Math.random().toString(36).slice(2)}`
    const emit = defineEmits(['removeWidget', 'toggleSplitScreen', 'editVisualization']);

    const props = defineProps<{
        report: any
        edit: boolean
        visualizations?: any[]
        textWidgetsIds?: string[]
    }>();

    const reportThemeName = ref(props.report?.report_theme_name || 'default');

    // --- Refs ---
    const gridstackContainer = ref<HTMLElement | null>(null);
    const grid = ref<GridStack | null>(null);
    const textWidgets = ref<any[]>([]);
    const displayedWidgets = ref<any[]>([]);
    const allTextWidgets = ref<any[]>([]);
    const allQueries = ref<any[]>([]);
    const vizById = ref<Record<string, any>>({});
    const queryById = ref<Record<string, any>>({});
    // Legacy widget mapping removed
    const stepCache = ref<Record<string, any>>({});
    const activeLayout = ref<any | null>(null);
    const layoutBlocks = ref<any[] | null>(null);
    const isLoading = ref<boolean>(true);
    let suppressGridReload = false;
    const editSnapshots = ref<Record<string, { x: number; y: number; width: number; height: number; content: string }>>({});

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
        const options: Array<{ label: string; value: string; selected: boolean }> = [];
        const reportTheme = props.report?.report_theme_name || props.report?.theme_name || 'default';

        // Add all theme options in their original order
        themeNames.forEach(themeName => {
            if (themeName === reportTheme) {
                // For the report's current theme, use empty value (represents clearing override)
                options.push({
                    label: themeName,
                    value: '',
                    selected: !themeOverride.value
                });
            } else {
                // For other themes, use the theme name as value
                options.push({
                    label: themeName,
                    value: themeName,
                    selected: themeOverride.value === themeName
                });
            }
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
        // Process displayedWidgets - preserve their type (text, card, column_layout, visualization, etc.)
        const regular = displayedWidgets.value.map(w => {
            // Only default to 'regular' if type is not already set or is 'visualization'
            const widgetType = w.type === 'visualization' ? 'regular' : (w.type || 'regular');
            return {
                ...w,
                type: widgetType,
                showControls: w.showControls ?? false,
                show_data: w.show_data ?? false,
                show_data_model: w.show_data_model ?? false
            };
        });
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
        await fetchActiveLayout();
        await loadQueriesForReport();
        await fetchAllWidgets();
        loadWidgetsIntoGrid(grid.value, allWidgets.value);
        isLoading.value = false;
        document.addEventListener('keydown', handleEscKey);
        // Cross-pane sync listeners
        window.addEventListener('dashboard:layout_changed', handleExternalLayoutChanged as any)
        window.addEventListener('query:default_step_changed', handleExternalDefaultStepChanged as any)
        window.addEventListener('visualization:updated', handleVisualizationUpdated as any)
    });

    onBeforeUnmount(() => {
        grid.value?.destroy(false);
        modalGrid.value?.destroy(false);
        document.removeEventListener('keydown', handleEscKey);
        window.removeEventListener('dashboard:layout_changed', handleExternalLayoutChanged as any)
        window.removeEventListener('query:default_step_changed', handleExternalDefaultStepChanged as any)
        window.removeEventListener('visualization:updated', handleVisualizationUpdated as any)
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
                minRow: 30,
                disableDrag: !props.edit,
                disableResize: !props.edit,
            }, gridstackContainer.value);

            grid.value.on('change', handleGridChange);
            grid.value.on('dragstop', handleGridStop);
            grid.value.on('resizestop', handleGridStop);
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
        // If layout blocks are hydrated with embedded payloads, skip extra fetch
        textWidgets.value = [];
        const hasEmbeddedText = Array.isArray(layoutBlocks.value) && layoutBlocks.value.some((b: any) => b?.type === 'text_widget' && b?.text_widget);
        if (!hasEmbeddedText) {
            await loadTextWidgetsForReport();
        }
        applyLayoutToLocalState();
    }

    async function loadTextWidgetsForReport() {
        try {
            const base = props.edit ? '/api/reports' : '/api/r'
            const { data, error } = await useMyFetch(`${base}/${props.report.id}/text_widgets`, { method: 'GET' });
            if (error.value) throw error.value;
            allTextWidgets.value = Array.isArray(data.value) ? data.value : [];
        } catch (e: any) {
            console.error('Failed to fetch text widgets:', e);
            allTextWidgets.value = [];
        }
    }

    async function fetchActiveLayout() {
        try {
            const base = props.edit ? '/api/reports' : '/api/r'
            const { data, error } = await useMyFetch(`${base}/${props.report.id}/layouts?hydrate=true`, { method: 'GET' });
            if (error.value) throw error.value;
            const layouts = Array.isArray(data.value) ? data.value : [];
            const found = layouts.find((l: any) => l.is_active);
            activeLayout.value = found || null;
            layoutBlocks.value = found?.blocks || [];
        } catch (e: any) {
            console.error('Failed to fetch active layout:', e);
            activeLayout.value = null;
            layoutBlocks.value = [];
        }
    }

    async function loadQueriesForReport() {
        try {
            const base = props.edit ? '/api/queries' : '/api/queries'
            const { data, error } = await useMyFetch(`${base}?report_id=${props.report.id}`, { method: 'GET' });
            if (error.value) throw error.value;
            const items = Array.isArray(data.value) ? data.value : [];
            allQueries.value = items;
            const qMap: Record<string, any> = {};
            const vMap: Record<string, any> = {};
            for (const q of items) {
                if (q?.id) qMap[q.id] = q;
                for (const v of (q?.visualizations || [])) {
                    if (v?.id) vMap[v.id] = v;
                }
            }
            queryById.value = qMap;
            vizById.value = vMap;
        } catch (e: any) {
            console.error('Failed to load queries:', e);
            allQueries.value = [];
            queryById.value = {};
            vizById.value = {};
        }
    }

    // Seed visualizations from props if provided (preferred path)
    watch(() => props.visualizations, (list) => {
        try {
            if (!Array.isArray(list)) return
            const map: Record<string, any> = {}
            for (const v of list) if (v?.id) map[v.id] = v
            if (Object.keys(map).length) vizById.value = { ...vizById.value, ...map }
        } catch {}
    }, { immediate: true, deep: true })

    async function ensureDefaultStepForQuery(queryId: string) {
        try {
            // If we already have a cached default step id and step, return it quickly
            const existingQ = queryById.value[queryId];
            const cachedDefaultId = existingQ?.default_step_id
            if (cachedDefaultId && stepCache.value[cachedDefaultId]) return stepCache.value[cachedDefaultId]

            // Always fetch current default step directly from backend to avoid stale local state
            const { data, error } = await useMyFetch(`/api/queries/${queryId}/default_step`, { method: 'GET' });
            if (error.value) throw error.value;
            const step = (data.value || {}).step || null;
            if (step && step.id) {
                // Cache by step id
                stepCache.value[step.id] = step;
                // Seed or update query map with latest default_step_id
                const prev = queryById.value[queryId] || { id: queryId } as any
                if (prev.default_step_id !== step.id) {
                    queryById.value = { ...queryById.value, [queryId]: { ...prev, default_step_id: step.id } } as any
                }
                return step;
            }

            // As a fallback, hydrate query map so future calls can succeed
            try {
                const qRes = await useMyFetch(`/api/queries/${queryId}`, { method: 'GET' })
                const qData: any = qRes?.data?.value
                if (qData?.id) {
                    queryById.value = { ...queryById.value, [queryId]: qData } as any
                }
            } catch {}
            return null;
        } catch (e: any) {
            console.error('Failed to load default step for query', queryId, e);
            return null;
        }
    }

    async function applyLayoutToLocalState() {
        // Wait until layout is fetched to avoid showing all widgets prematurely
        if (layoutBlocks.value === null) {
            return;
        }
        // If we have blocks, strictly use them to decide what to render and where
        if (Array.isArray(layoutBlocks.value) && layoutBlocks.value.length > 0) {
            const blocks = layoutBlocks.value;
            const textMap = new Map((allTextWidgets.value || []).map((tw: any) => [tw.id, tw]));
            const nextDisplayed: any[] = [];
            const nextText: any[] = [];
            const stepPromises: Promise<any>[] = [];

            // Helper to recursively collect all visualization IDs from nested blocks
            const collectVisualizationIds = (blockList: any[]): string[] => {
                const ids: string[] = [];
                for (const b of blockList) {
                    if (b.type === 'visualization' && b.visualization_id) {
                        ids.push(b.visualization_id);
                    }
                    if (b.children && Array.isArray(b.children)) {
                        ids.push(...collectVisualizationIds(b.children));
                    }
                    if (b.columns && Array.isArray(b.columns)) {
                        for (const col of b.columns) {
                            if (col.children && Array.isArray(col.children)) {
                                ids.push(...collectVisualizationIds(col.children));
                            }
                        }
                    }
                }
                return ids;
            };

            // Pre-fetch data for ALL visualizations (including nested ones)
            const allVizIds = collectVisualizationIds(blocks);
            const vizFetchPromises: Promise<any>[] = [];
            
            // First, fetch any missing visualization data
            for (const vid of allVizIds) {
                if (!vizById.value[vid]) {
                    // Visualization not in cache, try to fetch it
                    vizFetchPromises.push(
                        (async () => {
                            try {
                                const { data, error } = await useMyFetch(`/api/visualizations/${vid}`, { method: 'GET' });
                                if (!error.value && data.value) {
                                    const vizData = data.value as any;
                                    vizById.value = { ...vizById.value, [vid]: vizData };
                                    // Also cache the query if present
                                    if (vizData.query_id && !queryById.value[vizData.query_id]) {
                                        const qRes = await useMyFetch(`/api/queries/${vizData.query_id}`, { method: 'GET' });
                                        if (!qRes.error.value && qRes.data.value) {
                                            queryById.value = { ...queryById.value, [vizData.query_id]: qRes.data.value as any };
                                        }
                                    }
                                }
                            } catch {}
                        })()
                    );
                }
            }
            
            // Wait for visualization data to be fetched
            if (vizFetchPromises.length > 0) {
                await Promise.allSettled(vizFetchPromises);
            }
            
            // Now pre-fetch query/step data for all visualizations
            for (const vid of allVizIds) {
                const viz = vizById.value[vid];
                if (viz?.query_id) {
                    const qid = viz.query_id;
                    const q = queryById.value[qid];
                    const defaultStepId = q?.default_step_id;
                    if (!defaultStepId || !stepCache.value[defaultStepId]) {
                        stepPromises.push(ensureDefaultStepForQuery(qid));
                    }
                }
            }
            
            // IMPORTANT: Wait for step data BEFORE processing blocks
            // This ensures nested visualizations have their step data available
            if (stepPromises.length > 0) {
                await Promise.allSettled(stepPromises);
                stepPromises.length = 0; // Clear after awaiting
            }

            // Helper to override block coords with current GridStack engine node if present
            const resolveCoords = (id: string, fallback: { x: number; y: number; width: number; height: number }) => {
                try {
                    const node = grid.value?.engine.nodes.find(n => String(n.id) === id);
                    if (node) {
                        return { x: node.x, y: node.y, width: node.w, height: node.h };
                    }
                } catch {}
                return fallback;
            }

            for (const b of blocks) {
                if (b.type === 'text_widget' && b.text_widget_id) {
                    // Legacy text widget with DB reference
                    const embedded = (b as any).text_widget || null;
                    const baseSrc = embedded || textMap.get(b.text_widget_id) || { id: b.text_widget_id, content: '', isEditing: false, isNew: false, showControls: false };
                    const coords = resolveCoords(String(baseSrc.id), { x: b.x, y: b.y, width: b.width, height: b.height })
                    nextText.push({
                        ...baseSrc,
                        x: coords.x, y: coords.y, width: coords.width, height: coords.height,
                        // carry per-block view overrides for text widgets as well
                        layout_view_overrides: (b as any).view_overrides || null,
                        type: 'text',
                        isEditing: baseSrc.isEditing ?? false,
                        isNew: baseSrc.isNew ?? false,
                        showControls: baseSrc.showControls ?? false,
                    });
                } else if ((b.type === 'text' || b.type === 'text_widget') && (b as any).content) {
                    // Inline text block (AI generated, no DB reference)
                    // Handles both type: "text" (new) and type: "text_widget" with content (legacy)
                    const blockId = `inline-text-${nextDisplayed.length + nextText.length}`;
                    const coords = resolveCoords(blockId, { x: b.x, y: b.y, width: b.width, height: b.height })
                    nextDisplayed.push({
                        id: blockId,
                        x: coords.x, y: coords.y, width: coords.width, height: coords.height,
                        type: 'text',
                        content: (b as any).content,
                        variant: (b as any).variant,
                        showControls: false,
                    });
                } else if (b.type === 'card') {
                    // Card block with children
                    const blockId = `card-${nextDisplayed.length}`;
                    const coords = resolveCoords(blockId, { x: b.x, y: b.y, width: b.width, height: b.height })
                    nextDisplayed.push({
                        id: blockId,
                        x: coords.x, y: coords.y, width: coords.width, height: coords.height,
                        type: 'card',
                        chrome: (b as any).chrome,
                        children: (b as any).children || [],
                        showControls: false,
                    });
                } else if (b.type === 'column_layout') {
                    // Column layout block
                    const blockId = `columns-${nextDisplayed.length}`;
                    const coords = resolveCoords(blockId, { x: b.x, y: b.y, width: b.width, height: b.height })
                    nextDisplayed.push({
                        id: blockId,
                        x: coords.x, y: coords.y, width: coords.width, height: coords.height,
                        type: 'column_layout',
                        columns: (b as any).columns || [],
                        showControls: false,
                    });
                } else if (b.type === 'visualization' && (b as any).visualization_id) {
                    const vid = (b as any).visualization_id as string;
                    const embedded = (b as any).visualization || null;
                    const viz = embedded || vizById.value[vid] || null;
                    if (!viz) {
                        // No viz found yet; skip for now
                        continue;
                    }
                    const coords = resolveCoords(String(vid), { x: b.x, y: b.y, width: b.width, height: b.height })
                    const qid = viz.query_id;
                    const q = queryById.value[qid] || null;
                    let step: any = null;
                    const defaultStepId = q?.default_step_id;
                    if (defaultStepId && stepCache.value[defaultStepId]) {
                        step = stepCache.value[defaultStepId];
                    } else if (qid) {
                        // fetch asynchronously and re-apply later
                        stepPromises.push(ensureDefaultStepForQuery(qid));
                    }
                    // Merge view overrides (layout should be able to override viz.view)
                    const mergedView = (() => {
                        const v = viz.view || {};
                        const o = (b as any).view_overrides || null;
                        // final view: viz.view overlaid by layout overrides
                        return o ? { ...v, ...o } : v;
                    })();
                    nextDisplayed.push({
                        id: vid,
                        x: coords.x, y: coords.y, width: coords.width, height: coords.height,
                        type: 'regular',
                        isVisualization: true,
                        query_id: qid,
                        title: viz.title || '',
                        last_step: step || (defaultStepId ? stepCache.value[defaultStepId] : null),
                        view: mergedView,
                        showControls: false,
                        show_data: false,
                        show_data_model: false,
                    });
                }
            }

            displayedWidgets.value = nextDisplayed;
            textWidgets.value = nextText;
            if (stepPromises.length > 0) {
                // After steps load, re-apply to inject last_step
                try { await Promise.allSettled(stepPromises); } catch {}
                // Re-run quickly without refetching layout
                const prev = layoutBlocks.value; layoutBlocks.value = prev; // trigger
                // Recompute
                await nextTick();
                await applyLayoutToLocalState();
            }
            return;
        }

    }

    async function refreshLayout() {
        await fetchActiveLayout();
        applyLayoutToLocalState();
        await loadWidgetsIntoGrid(grid.value, allWidgets.value);
        if (isModalOpen.value && modalGrid.value) {
            await loadWidgetsIntoGrid(modalGrid.value, allWidgets.value, true);
        }
    }

    // External event handlers
    async function handleExternalLayoutChanged(ev: CustomEvent) {
        try {
            const detail: any = (ev as any)?.detail || {}
            if (detail && String(detail.report_id || props.report?.id) !== String(props.report?.id)) {
                // Ignore events for other reports
                return
            }
            if (detail?.source && detail.source === instanceId) {
                // Ignore our own broadcast to prevent races with local persistence
                return
            }
            await refreshLayout()
        } catch {}
    }

    async function handleExternalDefaultStepChanged(ev: CustomEvent) {
        try {
            const detail: any = (ev as any)?.detail || {}
            const qid = detail?.query_id
            const step = detail?.step
            if (!qid) return
            if (step?.id) {
                stepCache.value[step.id] = step
            }
            const prevQ = queryById.value[qid] || { id: qid }
            if (prevQ.default_step_id !== step?.id) {
                queryById.value = { ...queryById.value, [qid]: { ...prevQ, default_step_id: step?.id || prevQ.default_step_id } } as any
            }
            // Force tiles to re-bind last_step for any viz or widget bound to this query
            const prev = layoutBlocks.value; layoutBlocks.value = prev
            await nextTick()
            await applyLayoutToLocalState()
        } catch {}
    }

    function handleVisualizationUpdated(ev: CustomEvent) {
        try {
            const detail: any = (ev as any)?.detail || {}
            const id: string | undefined = detail?.id
            const updated: any = detail?.visualization
            if (!id || !updated) return
            // Update local viz map and any displayed tile that matches
            if (vizById.value[id]) {
                vizById.value = { ...vizById.value, [id]: { ...vizById.value[id], ...updated } }
            }
            // Mutate displayed widget view/title in place to avoid grid reload and preserve position
            const target = displayedWidgets.value.find(w => w.id === id && w.isVisualization)
            if (target) {
                if (Object.prototype.hasOwnProperty.call(updated, 'title')) target.title = updated.title
                if (Object.prototype.hasOwnProperty.call(updated, 'view')) target.view = updated.view
            }
        } catch {}
    }

    async function getTextWidgetsInternal() {
        await loadTextWidgetsForReport();
        applyLayoutToLocalState();
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

    // Remove legacy widgets watcher; tiles come from layout + visualizations

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
            // Broadcast theme change so other panes (chat preview/editor) update live
            try {
                window.dispatchEvent(new CustomEvent('dashboard:theme_changed', { detail: { report_id: props.report?.id, themeName: val, overrides: reportOverridesRef.value || null } }))
            } catch {}
        } catch (e: any) {
            console.error('Failed to update report theme', e);
            toast.add({ title: 'Failed to save theme', description: e?.message || String(e), color: 'red' });
        }
    });
 
    watch(allWidgets, async (currentWidgets, oldWidgets) => {
        if (suppressGridReload) return;
        // Simplified condition: Reload if length changes or if it's a deep change (heuristically)
        if (currentWidgets.length !== oldWidgets?.length || JSON.stringify(currentWidgets) !== JSON.stringify(oldWidgets)) {
            await loadWidgetsIntoGrid(grid.value, currentWidgets);
            if (isModalOpen.value && modalGrid.value) {
                await loadWidgetsIntoGrid(modalGrid.value, currentWidgets, true);
            }
        }
    }, { deep: true });


    // --- Gridstack Event Handlers ---
    const handleGridChange = async (event: Event, items: any[]) => {
        if (!props.edit) return;
        items.forEach(item => {
            const nodeId = typeof item.id === 'string' ? item.id : (item?.el?.getAttribute?.('gs-id') || String(item.id))
            // Update both displayedWidgets/textWidgets directly to keep local state authoritative
            const textIdx = textWidgets.value.findIndex(w => String(w.id) === String(nodeId))
            if (textIdx !== -1) {
                const tw = textWidgets.value[textIdx]
                tw.x = item.x; tw.y = item.y; tw.width = item.w; tw.height = item.h
                return
            }
            const dispIdx = displayedWidgets.value.findIndex(w => String(w.id) === String(nodeId))
            if (dispIdx !== -1) {
                const dw = displayedWidgets.value[dispIdx]
                dw.x = item.x; dw.y = item.y; dw.width = item.w; dw.height = item.h
                return
            }
        });
    };

    // Ensure we also persist when the user stops dragging/resizing a single item
    // Debounced full-layout saver to avoid races with subsequent operations (like deletions)
    const handleGridStop = async (event: Event, el: HTMLElement) => {
        if (!props.edit || !grid.value || !el) return;
        // Mirror the latest node back to our local widget for consistency
        const node = grid.value.engine.nodes.find(n => n.el === el);
        if (node) {
            const id = typeof node.id === 'string' ? node.id : (el.getAttribute('gs-id') || String(node.id));
            const w = findWidgetById(id);
            if (w) {
                if (w.type === 'text' && w.isEditing) {
                    w.x = node.x; w.y = node.y; w.width = node.w; w.height = node.h;
                    return;
                }
                w.x = node.x; w.y = node.y; w.width = node.w; w.height = node.h;
            }
        }
        schedulePersistFullLayout()
    };

    // Debounced full layout persistence (for removals and auto-reflow)
    let fullSaveTimer: number | null = null
    async function persistFullLayout() {
        if (!props.edit || !grid.value) return;
        try {
            // Ensure we have the active layout id for full replacement
            if (!activeLayout.value?.id) {
                await fetchActiveLayout();
            }
            const layoutId = activeLayout.value?.id;
            if (!layoutId) return;

            const nodes = grid.value.engine.nodes || [];
            const blocks: any[] = [];
            for (const node of nodes) {
                const id = typeof node.id === 'string' ? node.id : (node?.el?.getAttribute?.('gs-id') || String(node.id));
                const w = findWidgetById(id);
                if (!w) continue;
                
                const basePos = { x: node.x, y: node.y, width: node.w, height: node.h };
                
                // Handle all block types properly
                if (w.type === 'text' && w.content) {
                    // Inline AI-generated text block (no DB reference)
                    blocks.push({ 
                        type: 'text', 
                        content: w.content,
                        variant: w.variant,
                        ...basePos 
                    });
                } else if (w.type === 'text' && !w.content && !w.isNew) {
                    // Legacy text widget with DB reference
                    blocks.push({ 
                        type: 'text_widget', 
                        text_widget_id: w.id, 
                        ...basePos 
                    });
                } else if (w.type === 'card') {
                    // Card block with children
                    blocks.push({ 
                        type: 'card', 
                        chrome: w.chrome,
                        children: w.children || [],
                        ...basePos 
                    });
                } else if (w.type === 'column_layout') {
                    // Column layout block
                    blocks.push({ 
                        type: 'column_layout', 
                        columns: w.columns || [],
                        ...basePos 
                    });
                } else if (w.type === 'regular' || w.type === 'visualization' || w.isVisualization) {
                    // Visualization block
                    blocks.push({ 
                        type: 'visualization', 
                        visualization_id: w.id,
                        view_overrides: w.layout_view_overrides || null,
                        ...basePos 
                    });
                }
                // Skip unknown types and unsaved new widgets
            }
            const { data, error } = await useMyFetch(`/api/reports/${props.report.id}/layouts/${layoutId}`, { method: 'PATCH', body: { blocks } });
            if (error.value) throw error.value;
            // Update local copies to reflect latest server state
            if (data?.value) {
                activeLayout.value = data.value as any;
                layoutBlocks.value = (activeLayout.value as any)?.blocks || [];
            }
        } catch (e: any) {
            console.error('Failed to persist full layout', e)
        }
    }
    function schedulePersistFullLayout() {
        if (fullSaveTimer) window.clearTimeout(fullSaveTimer)
        fullSaveTimer = window.setTimeout(() => { persistFullLayout() }, 180)
    }

    const handleGridAdded = (event: Event, items: any[]) => {
        // Usually triggered by makeWidget or addWidget. Log if needed for debugging.
        // console.log(`Grid added event: ${items.map(i=>i.id).join(', ')}`);
    };

    const handleGridRemoved = (event: Event, items: any[]) => {
        // Sync local data state AFTER gridstack removes the element
        suppressGridReload = true;
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
        // After GridStack compacts, mirror engine nodes back into our data model
        try {
            const nodes = grid.value?.engine.nodes || [];
            for (const node of nodes) {
                const id = typeof node.id === 'string' ? node.id : (node?.el?.getAttribute?.('gs-id') || String(node.id));
                const w = findWidgetById(id);
                if (!w) continue;
                w.x = node.x; w.y = node.y; w.width = node.w; w.height = node.h;
            }
        } catch {}
        // Persist the new positions of remaining widgets after GridStack reflow
        schedulePersistFullLayout()
        suppressGridReload = false;
    };

    // --- Widget Find & Update ---
    const findWidgetById = (id: string): any | undefined => {
        const cleanId = id?.startsWith('modal-') ? id.substring(6) : id;
        return allWidgets.value.find(w => w.id === cleanId);
    };

    // Removed legacy backend updates for direct widget/text positions; layout is source of truth

    // --- Widget CRUD ---
    async function removeWidget(widget: any) {
        try {
            const el = grid.value?.engine.nodes.find(n => n.id === widget.id)?.el;
            if (el && grid.value) {
                 grid.value.removeWidget(el); // Triggers 'removed' event
                await nextTick()
                await persistFullLayout()
            } else {
                const index = displayedWidgets.value.findIndex(w => w.id === widget.id);
                if (index !== -1) displayedWidgets.value.splice(index, 1);
                 emit('removeWidget', { id: widget.id });
                // If no grid event will fire, persist layout now
                try {
                    // Mirror engine nodes first to avoid watchers resetting positions
                    suppressGridReload = true;
                    const nodes = grid.value?.engine?.nodes || [];
                    for (const node of nodes) {
                        const id2 = typeof node.id === 'string' ? node.id : (node?.el?.getAttribute?.('gs-id') || String(node.id));
                        const w2 = findWidgetById(id2);
                        if (!w2) continue;
                        w2.x = node.x; w2.y = node.y; w2.width = node.w; w2.height = node.h;
                    }
                } catch {}
                await persistFullLayout()
                suppressGridReload = false;
            }
            toast.add({ title: 'Removed from dashboard' });
            // Broadcast so previews update their membership buttons immediately
            try {
                window.dispatchEvent(new CustomEvent('dashboard:layout_changed', { detail: { report_id: props.report.id, action: 'removed', widget_id: widget.id, source: instanceId } }))
            } catch {}
        } catch (error: any) {
            console.error(`Failed to remove from dashboard ${widget.id}`, error);
            toast.add({ title: 'Error', description: `Failed to remove from dashboard. ${error.message || ''}`, color: 'red' });
        }
    }
    async function removeTextWidget(widget: any) {
        try {
            // Check if it's an inline text block (has content, in displayedWidgets)
            if (widget.content) {
                const idx = displayedWidgets.value.findIndex(w => w.id === widget.id && w.type === 'text');
                if (idx !== -1) {
                    displayedWidgets.value.splice(idx, 1);
                }
                // Remove from grid
                const el = grid.value?.engine.nodes.find(n => n.id === widget.id)?.el;
                if (el && grid.value) {
                    grid.value.removeWidget(el, false, false);
                }
                schedulePersistFullLayout();
                toast.add({ title: 'Text block removed' });
                return;
            }
            
            // Legacy text widget - delete from DB
            if (!widget.isNew) {
                const { error } = await useMyFetch(`/api/reports/${props.report.id}/text_widgets/${widget.id}`, { method: 'DELETE' });
                // Treat 404 as success: widget may already be deleted; backend also cleans layout
                if (error.value) {
                    const status = (error.value as any)?.status || (error.value as any)?.response?.status;
                    if (status !== 404) throw error.value;
                }
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
                // If no grid event will fire, persist layout now
                schedulePersistFullLayout()
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
            x: undefined as any, y: undefined as any, width: 6, height: 7,
            type: 'text', isEditing: true, isNew: true, showControls: true
        };

        // Before adding, mirror current engine node positions back into local state
        try {
            const nodes = grid.value.engine?.nodes || [];
            for (const node of nodes) {
                const id = typeof node.id === 'string' ? node.id : (node?.el?.getAttribute?.('gs-id') || String(node.id));
                const w = findWidgetById(id);
                if (!w) continue;
                w.x = node.x; w.y = node.y; w.width = node.w; w.height = node.h;
            }
        } catch {}
        // Persist current layout so any future refresh uses the latest positions
        schedulePersistFullLayout()

        suppressGridReload = true;
        textWidgets.value.push(newWidget);

        await nextTick();

        const element = document.querySelector(`[gs-id="${tempId}"]`);
        if (element && grid.value) {
            // Let GridStack choose the first available slot without moving others
            grid.value.addWidget(element as HTMLElement, { id: tempId, w: newWidget.width, h: newWidget.height, autoPosition: true } as any);
        } else {
            console.warn(`Could not find DOM element for new widget ${tempId} immediately after adding.`);
        }
        suppressGridReload = false;
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
                         // Ensure placement matches temporary computed position to avoid reflow
                         grid.value.addWidget(newElement as HTMLElement, { id: savedWidget.id, x: savedWidget.x, y: savedWidget.y, w: savedWidget.width, h: savedWidget.height, autoPosition: false } as any);
                    } else {
                         console.warn(`Could not find new element ${savedWidget.id} in DOM to add to gridstack.`);
                         // Consider fallback: await loadWidgetsIntoGrid(grid.value, allWidgets.value);
                    }

                    // Also patch active layout with the new text widget position
                    try {
                        const { error: layoutErr } = await useMyFetch(`/api/reports/${props.report.id}/layouts/active/blocks`, {
                            method: 'PATCH',
                            body: { blocks: [{ type: 'text_widget', text_widget_id: savedWidget.id, x: savedWidget.x, y: savedWidget.y, width: savedWidget.width, height: savedWidget.height }] }
                        });
                        if (layoutErr.value) throw layoutErr.value;
                    } catch (e: any) {
                        console.error('Failed to add new text widget to layout', e);
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
            if (editSnapshots.value[existingWidget.id]) delete editSnapshots.value[existingWidget.id];
            await updateWidgetBackend(existingWidget);
        }
    };

    async function updateWidgetBackend(widget: any) {
        try {
            const { error } = await useMyFetch(`/api/reports/${props.report.id}/text_widgets/${widget.id}`, {
                method: 'PUT',
                body: { content: widget.content, x: widget.x, y: widget.y, width: widget.width, height: widget.height }
            });
            if (error.value) throw error.value;
            // Keep layout in sync with latest position
            try {
                await useMyFetch(`/api/reports/${props.report.id}/layouts/active/blocks`, {
                    method: 'PATCH',
                    body: { blocks: [{ type: 'text_widget', text_widget_id: widget.id, x: widget.x, y: widget.y, width: widget.width, height: widget.height }] }
                });
            } catch {}
            toast.add({ title: 'Text Widget Saved' });
        } catch (e: any) {
            console.error('Failed to update text widget', e);
            toast.add({ title: 'Error', description: `Failed to update text widget. ${e?.message || ''}`, color: 'red' });
        }
    }

    const toggleTextEdit = (widget: any) => {
        if (widget.isNew) {
            removeTextWidget(widget);
            return;
        }
        
        // Check if it's an inline text block (has content, in displayedWidgets)
        if (widget.content) {
            const idx = displayedWidgets.value.findIndex(w => w.id === widget.id && w.type === 'text');
            if (idx !== -1) {
                const w = displayedWidgets.value[idx];
                const nowEditing = !w.isEditing;
                if (nowEditing && !editSnapshots.value[w.id]) {
                    editSnapshots.value[w.id] = { x: w.x, y: w.y, width: w.width, height: w.height, content: w.content };
                }
                w.isEditing = nowEditing;
                return;
            }
        }
        
        // Legacy text widget (in textWidgets)
        const idx = textWidgets.value.findIndex(w => w.id === widget.id);
        if (idx !== -1) {
            const w = textWidgets.value[idx];
            const nowEditing = !w.isEditing;
            if (nowEditing && !editSnapshots.value[w.id]) {
                editSnapshots.value[w.id] = { x: w.x, y: w.y, width: w.width, height: w.height, content: w.content };
            }
            w.isEditing = nowEditing;
        } else {
            console.warn(`Could not find text widget with ID ${widget.id} to toggle edit state.`);
        }
    };

    const cancelTextEdit = (widget: any) => {
        if (widget.isNew) {
             removeTextWidget(widget);
             return;
        }
        
        // Check if it's an inline text block (has content, in displayedWidgets)
        if (widget.content) {
            const idx = displayedWidgets.value.findIndex(w => w.id === widget.id && w.type === 'text');
            if (idx !== -1) {
                const w = displayedWidgets.value[idx];
                const snap = editSnapshots.value[w.id];
                w.isEditing = false;
                if (snap) {
                    w.content = snap.content;
                    delete editSnapshots.value[w.id];
                }
                return;
            }
        }
        
        // Legacy text widget (in textWidgets)
        const idx = textWidgets.value.findIndex(w => w.id === widget.id);
        if (idx !== -1) {
            const w = textWidgets.value[idx];
            const snap = editSnapshots.value[w.id];
            w.isEditing = false;
            if (snap) {
                w.content = snap.content;
                w.x = snap.x; w.y = snap.y; w.width = snap.width; w.height = snap.height;
                try {
                    const el = grid.value?.engine.nodes.find(n => n.id === w.id)?.el as HTMLElement | undefined;
                    if (el && grid.value) {
                        grid.value.update(el as HTMLElement, { id: w.id, x: snap.x, y: snap.y, w: snap.width, h: snap.height, autoPosition: false } as any);
                    }
                } catch {}
                delete editSnapshots.value[w.id];
            }
        }
    };

    // Save inline text block content
    async function saveInlineTextBlock(widget: any, content: string) {
        // First check if it's a NEW text widget (in textWidgets array, has isNew flag)
        const textIdx = textWidgets.value.findIndex(w => w.id === widget.id);
        if (textIdx !== -1 && textWidgets.value[textIdx].isNew) {
            // This is a new text widget being saved - use the legacy save flow
            await saveTextWidget(content, textWidgets.value[textIdx]);
            return;
        }
        
        // Search in displayedWidgets by id
        let idx = displayedWidgets.value.findIndex(w => w.id === widget.id);
        
        // If not found by id, try to find any text block that's being edited
        if (idx === -1) {
            idx = displayedWidgets.value.findIndex(w => w.type === 'text' && w.isEditing);
        }
        
        // Also check textWidgets for non-new items being edited
        if (idx === -1) {
            const textEditingIdx = textWidgets.value.findIndex(w => w.isEditing && !w.isNew);
            if (textEditingIdx !== -1) {
                // Use legacy save flow
                await saveTextWidget(content, textWidgets.value[textEditingIdx]);
                return;
            }
        }
        
        if (idx !== -1) {
            displayedWidgets.value[idx].content = content;
            displayedWidgets.value[idx].isEditing = false;
            if (editSnapshots.value[displayedWidgets.value[idx].id]) {
                delete editSnapshots.value[displayedWidgets.value[idx].id];
            }
            // Persist to layout
            await persistFullLayout();
            toast.add({ title: 'Text saved' });
        } else {
            console.warn('Could not find widget to save:', widget.id);
            toast.add({ title: 'Error', description: 'Could not save text block', color: 'red' });
        }
    }

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
        // Ensure modal renders from latest layout-driven positions
        await refreshLayout();
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

    // --- Block Data Resolution (for nested blocks in cards/columns) ---
    // Check if a card block contains a metric_card visualization (to hide duplicate header)
    function cardContainsMetricCard(cardWidget: any): boolean {
        const children = cardWidget?.children || [];
        for (const child of children) {
            if (child.type === 'visualization' && child.visualization_id) {
                const viz = vizById.value[child.visualization_id];
                const viewType = viz?.view?.view?.type || viz?.view?.type;
                if (viewType === 'metric_card' || viewType === 'count') {
                    return true;
                }
            }
        }
        return false;
    }

    function getWidgetDataForBlock(block: any): any {
        if (!block) return undefined;
        
        // If it's a visualization block, resolve from vizById
        if (block.type === 'visualization' && block.visualization_id) {
            const vid = block.visualization_id;
            const viz = vizById.value[vid];
            if (!viz) return undefined;
            
            const qid = viz.query_id;
            const q = queryById.value[qid] || null;
            const defaultStepId = q?.default_step_id;
            const step = defaultStepId ? stepCache.value[defaultStepId] : null;
            
            // Merge view overrides
            const mergedView = (() => {
                const v = viz.view || {};
                const o = block.view_overrides || null;
                return o ? { ...v, ...o } : v;
            })();
            
            return {
                id: vid,
                type: 'regular',
                isVisualization: true,
                query_id: qid,
                title: viz.title || '',
                last_step: step,
                view: mergedView,
                showControls: false,
                show_data: false,
                show_data_model: false,
            };
        }
        
        // If it's a text block with inline content
        if ((block.type === 'text' || block.type === 'text_widget') && block.content) {
            return {
                type: 'text',
                content: block.content,
                variant: block.variant,
            };
        }
        
        // For other block types, return the block as-is
        return block;
    }

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
        'heatmap', 'map', 'candlestick', 'treemap', 'radar_chart', 'metric_card', 'count'
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
        // Support v2 schema (view.view.type) and legacy (view.type, data_model.type)
        const viewObj = widget?.view
        const vType = viewObj?.view?.type || viewObj?.type
        const dmType = widget?.last_step?.data_model?.type
        return getCompForType(vType || dmType);
    }

    // --- Edit Visualization Handler ---
    function handleEditVisualization(payload: { queryId: string; widget: any }) {
        // Emit event to parent component to open query editor
        emit('editVisualization', {
            queryId: payload.queryId,
            stepId: payload.widget.last_step?.id || null,
            initialCode: payload.widget.last_step?.code || '',
            title: payload.widget.title || 'Edit Visualization'
        })
    }

    // --- Exposed Methods ---
    async function refreshTextWidgets() {
        await getTextWidgetsInternal();
        // After fetching, ensure the grid reflects the latest state of allWidgets
        await loadWidgetsIntoGrid(grid.value, allWidgets.value);
        if (isModalOpen.value && modalGrid.value) {
            await loadWidgetsIntoGrid(modalGrid.value, allWidgets.value, true);
        }
    }

    defineExpose({
        refreshTextWidgets,
        refreshLayout
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
      background-color: transparent;
      color: #2c3e50;
      text-align: left;
      overflow: hidden !important; /* CRITICAL: Prevent content spillover */
      position: absolute; /* Needed for Gridstack sizing */
      top: 0; bottom: 0; left: 0; right: 0; /* Fill the item */
      display: flex;
      flex-direction: column;
    }
    
    /* Ensure direct children of grid-stack-item-content fill available space */
    .grid-stack-item-content > * {
      flex: 1;
      min-height: 0;
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