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

                        <template v-if="widget.type === 'text'">
                            <TextWidgetView
                                :widget="widget"
                                :themeName="themeOverride || report?.report_theme_name || report?.theme_name"
                                :reportOverrides="report?.theme_overrides"
                                @save="(content) => saveTextWidget(content, widget)"
                                @cancel="cancelTextEdit(widget)"
                            />
                        </template>
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
            <div v-if="allWidgets.length === 0" class="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
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
    import { resolveEntryByType } from '@/components/dashboard/registry'
    import { themes } from '@/components/dashboard/themes'
    import { useDashboardTheme } from '@/components/dashboard/composables/useDashboardTheme'

    const toast = useToast();
    const emit = defineEmits(['removeWidget', 'toggleSplitScreen', 'editVisualization']);

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
    const allTextWidgets = ref<any[]>([]);
    const allQueries = ref<any[]>([]);
    const vizById = ref<Record<string, any>>({});
    const queryById = ref<Record<string, any>>({});
    const queryIdByWidgetId = ref<Record<string, string>>({});
    const stepCache = ref<Record<string, any>>({});
    const activeLayout = ref<any | null>(null);
    const layoutBlocks = ref<any[] | null>(null);

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
        await fetchActiveLayout();
        await loadQueriesForReport();
        await fetchAllWidgets();
        loadWidgetsIntoGrid(grid.value, allWidgets.value);
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
            const wMap: Record<string, string> = {};
            for (const q of items) {
                if (q?.id) qMap[q.id] = q;
                if (q?.widget_id) wMap[q.widget_id] = q.id;
                for (const v of (q?.visualizations || [])) {
                    if (v?.id) vMap[v.id] = v;
                }
            }
            queryById.value = qMap;
            vizById.value = vMap;
            queryIdByWidgetId.value = wMap;
        } catch (e: any) {
            console.error('Failed to load queries:', e);
            allQueries.value = [];
            queryById.value = {};
            vizById.value = {};
            queryIdByWidgetId.value = {};
        }
    }

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
            const widgetMap = new Map((props.widgets || []).map((w: any) => [w.id, w]));
            const textMap = new Map((allTextWidgets.value || []).map((tw: any) => [tw.id, tw]));
            const nextDisplayed: any[] = [];
            const nextText: any[] = [];
            const stepPromises: Promise<any>[] = [];

            for (const b of blocks) {
                if (b.type === 'widget' && b.widget_id) {
                    const src = widgetMap.get(b.widget_id);
                    if (!src) continue;
                    // Try to resolve the latest default step for the widget via its query
                    const qidForWidget = queryIdByWidgetId.value[b.widget_id];
                    let lastStepForWidget: any = null;
                    if (qidForWidget) {
                        const qExisting = queryById.value[qidForWidget];
                        const defaultStepId = qExisting?.default_step_id;
                        if (defaultStepId && stepCache.value[defaultStepId]) {
                            lastStepForWidget = stepCache.value[defaultStepId];
                        } else {
                            stepPromises.push(ensureDefaultStepForQuery(qidForWidget));
                        }
                    }
                    nextDisplayed.push({
                        ...src,
                        x: b.x, y: b.y, width: b.width, height: b.height,
                        // carry per-block view overrides so layout styling can win over step.view
                        layout_view_overrides: (b as any).view_overrides || null,
                        type: 'regular',
                        showControls: src.showControls ?? false,
                        show_data: src.show_data ?? false,
                        show_data_model: src.show_data_model ?? false,
                        last_step: lastStepForWidget || src.last_step || null,
                    });
                } else if (b.type === 'text_widget' && b.text_widget_id) {
                    const embedded = (b as any).text_widget || null;
                    const baseSrc = embedded || textMap.get(b.text_widget_id) || { id: b.text_widget_id, content: '', isEditing: false, isNew: false, showControls: false };
                    nextText.push({
                        ...baseSrc,
                        x: b.x, y: b.y, width: b.width, height: b.height,
                        // carry per-block view overrides for text widgets as well
                        layout_view_overrides: (b as any).view_overrides || null,
                        type: 'text',
                        isEditing: baseSrc.isEditing ?? false,
                        isNew: baseSrc.isNew ?? false,
                        showControls: baseSrc.showControls ?? false,
                    });
                } else if (b.type === 'visualization' && (b as any).visualization_id) {
                    const vid = (b as any).visualization_id as string;
                    const embedded = (b as any).visualization || null;
                    const viz = embedded || vizById.value[vid] || null;
                    if (!viz) {
                        // No viz found yet; skip for now
                        continue;
                    }
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
                        return o ? { ...v, ...o } : v;
                    })();
                    nextDisplayed.push({
                        id: vid,
                        x: b.x, y: b.y, width: b.width, height: b.height,
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
            // Replace displayed widget view/title immediately
            displayedWidgets.value = displayedWidgets.value.map(w => {
                if (w.id === id && w.isVisualization) {
                    // Ensure component resolver sees the new type immediately
                    const next = { ...w, title: updated.title ?? w.title, view: updated.view ?? w.view }
                    // Force change detection for nested consumers
                    return JSON.parse(JSON.stringify(next))
                }
                return w
            })
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

    watch(() => props.widgets, async () => {
        applyLayoutToLocalState();
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
            const widget = findWidgetById(nodeId);
            if (!widget) {
                 console.warn(`Widget with ID ${nodeId} not found in local data during grid change.`);
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
            }
        });
    };

    // Ensure we also persist when the user stops dragging/resizing a single item
    // Debounced saver to avoid duplicate/cancelled requests
    let saveTimer: number | null = null
    const handleGridStop = async (event: Event, el: HTMLElement) => {
        if (!props.edit || !grid.value || !el) return;
        const node = grid.value.engine.nodes.find(n => n.el === el);
        if (!node) return;
        const id = typeof node.id === 'string' ? node.id : (el.getAttribute('gs-id') || String(node.id));
        const w = findWidgetById(id);
        if (!w || w.isNew) return;

        const patch = w.type === 'text'
            ? { type: 'text_widget', text_widget_id: w.id, x: node.x, y: node.y, width: node.w, height: node.h }
            : { type: 'visualization', visualization_id: w.id, x: node.x, y: node.y, width: node.w, height: node.h };
        if (saveTimer) window.clearTimeout(saveTimer)
        saveTimer = window.setTimeout(async () => {
            try {
                const { error } = await useMyFetch(`/api/reports/${props.report.id}/layouts/active/blocks`, { method: 'PATCH', body: { blocks: [patch] } });
                if (error.value) throw error.value;
            } catch (e: any) {
                console.error('Failed to save layout on stop', e);
            }
        }, 120)
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

    // Removed legacy backend updates for direct widget/text positions; layout is source of truth

    // --- Widget CRUD ---
    async function removeWidget(widget: any) {
        try {
            // Remove the visualization block from the active layout
            if (!activeLayout.value) {
                await fetchActiveLayout();
            }
            const layoutId = activeLayout.value?.id;
            if (!layoutId) throw new Error('Active layout not found');

            const currentBlocks: any[] = Array.isArray(layoutBlocks.value) ? [...layoutBlocks.value] : (activeLayout.value?.blocks || []);
            const filteredBlocks = currentBlocks.filter((b: any) => {
                if (b?.type === 'visualization' && b?.visualization_id === widget.id) return false;
                if (b?.type === 'widget' && b?.widget_id === widget.id) return false;
                return true;
            });

            const { data, error } = await useMyFetch(`/api/reports/${props.report.id}/layouts/${layoutId}`, {
                method: 'PATCH',
                body: { blocks: filteredBlocks }
            });
            if (error.value) throw error.value;

            // Update local state
            activeLayout.value = data.value as any;
            layoutBlocks.value = (activeLayout.value as any)?.blocks || [];

            const el = grid.value?.engine.nodes.find(n => n.id === widget.id)?.el;
            if (el && grid.value) {
                 grid.value.removeWidget(el); // Triggers 'removed' event
            } else {
                const index = displayedWidgets.value.findIndex(w => w.id === widget.id);
                if (index !== -1) displayedWidgets.value.splice(index, 1);
                 emit('removeWidget', { id: widget.id });
            }
            toast.add({ title: 'Removed from dashboard' });
            // Broadcast so previews update their membership buttons immediately
            try {
                window.dispatchEvent(new CustomEvent('dashboard:layout_changed', { detail: { report_id: props.report.id, action: 'removed', widget_id: widget.id } }))
            } catch {}
        } catch (error: any) {
            console.error(`Failed to remove from dashboard ${widget.id}`, error);
            toast.add({ title: 'Error', description: `Failed to remove from dashboard. ${error.message || ''}`, color: 'red' });
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