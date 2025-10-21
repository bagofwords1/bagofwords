<template>

    
    <div class="flex flex-row h-screen overflow-y-hidden bg-white">
        <!-- Left side (Chat) -->


        <div :style="{ 
                width: isSplitScreen ? `${leftPanelWidth}px` : '100%',
                transform: isSplitScreen ? 'none' : 'translateX(0)',
                willChange: 'transform, width',
                transition: isResizing ? 'none' : 'all 0.2s cubic-bezier(0.4, 0, 0.2, 1)'
             }">

    <header class="sticky top-0 bg-white z-10 flex flex-row pt-1 h-[40px] border-gray-200 pb-1 pr-2" >
        <GoBackChevron />
        <h1 class="text-sm md:text-left text-center mt-1 w-[500px]">
            <span class="font-semibold text-sm">
                <input type="text" class="inline hover:bg-gray-100 p-1 pt-1 outline-none active:bg-gray-100 hover:cursor-pointer text-left w-full transition-all duration-300 ease-in-out transform motion-safe:hover:scale-[1.01]" v-if="report"
                    :class="{ 'animate-fade-in': shouldAnimateTitle }"
                    v-model="report.title" @keyup.enter="saveReportTitle" ref="reportTitleInput" />
            </span>
        </h1>
        <div class="gap-1 hidden md:flex justify-end flex-1">
            <button @click="toggleSplitScreen" class="p-1.5 rounded text-xl hover:bg-gray-100 flex items-center">
                <span class="inline-flex items-center">
                    <Icon name="heroicons:chart-pie" class="inline-block mr-2" /> 
                </span>
                <span class="text-sm"
                :class="isSplitScreen ? 'hidden' : 'inline'"
                >Dashboard</span>
            </button>
            <button class="p-1.5 rounded text-lg hover:bg-gray-100">
                <Icon name="heroicons:ellipsis-horizontal" />
            </button>
            <UTooltip text="Rerun">
                <button @click="rerunReport" class="hidden px-3 py-1 rounded bg-gray-50 border border-gray-200 text-xs hover:bg-gray-100 mr-4">
                    <Icon name="heroicons:arrow-path-rounded-square" class="mr-2" />
                </button>
            </UTooltip>

        </div>
    </header>
            <div class="flex flex-col h-full relative">
                <div class="flex-1 overflow-y-auto mt-4 pb-14 h-[calc(100vh-200px)]" ref="agentLogContainer">
                    <div class="pl-4 pr-2 pb-[3px]">
                        <div v-if="!isPageLoading && completions.length == 0" class="mx-auto w-full mt-32 fade-in" :class="isSplitScreen ? 'w-full' : 'md:w-1/2'">
                            <h1 class="text-4xl mb-4">🪴</h1>
                            <h1 class="text-lg font-semibold">Ask a question to get started.</h1>
                            <p class="text-gray-500 text-sm mt-3">Examples:</p>
                            <ul class="list-none list-inside">
                                <li class="text-gray-500 text-sm mt-3" v-for="data_source in report.data_sources.filter(ds => ds.conversation_starters?.length > 0 && ds.active) ">
                                    <button
                                    class="text-gray-500 hover:bg-gray-50 border border-gray-200 text-xs rounded-md p-1.5"
                                    @click="handleExampleClick(data_source.conversation_starters?.[0])">  
                                        <DataSourceIcon :type="data_source.type" class="h-3 inline mr-2" />
                                        {{ data_source.conversation_starters?.[0].split('\n')[0]  }}
                                    </button>
                                </li>
                            </ul>
                            <hr class="my-4">
                            <p class="text-gray-500 text-sm"><span class="font-semibold">Tip:</span> <br />
                                Use @ to explore data sources and memories<br /> and to mention them in your question.</p>


                        </div>
                        <div class="relative mx-auto" :class="isSplitScreen ? 'w-full' : 'md:w-1/2'">
                        <ul v-if="completions.length > 0" class="mx-auto w-full">
                            <li v-for="completion in completions" :key="completion.id" class="text-gray-700 mb-2 text-sm">

                                <CompletionMessageComponent
                                    :key="`${completion.id}-${completion._updateKey || 0}`"
                                    :completion="completion"
                                    :excel="isExcel"
                                    :reportId="report_id"
                                    :selectedWidgetId="selectedWidgetId"
                                    @update:selectedWidgetId="handleSelectedWidgetId"
                                    @addWidget="handleAddWidget"
                                />
                                
                            </li>
                        </ul>
                        <!-- Removed sticky bar; Stop now lives inside PromptBox -->
                        </div>
                    </div>
                </div>

                <div ref="scrollAnchor"></div>
                <div class="absolute bottom-28 font-bold text-center left-0 right-0" :class="isSplitScreen ? 'w-full' : 'md:w-1/2 mx-auto'">
                    We've upgraded our AI agent!<br />
                    <span class=" text-md font-normal text-gray-500">This report is in read-only mode.</span><br />
                    <span class="font-normal text-blue-500">Please <button class="text-blue-500" @click="createNewReport">start a new report</button></span>
                </div>
            </div>

        </div>

        <!-- Resizer -->
        <div v-if="isSplitScreen" 
             class="w-1 bg-gray-200 cursor-col-resize hover:bg-blue-500 active:bg-blue-700"
             @mousedown="startResize">
        </div>

        <!-- Right side (White space) -->
        <div v-if="isSplitScreen" 
             :style="{ 
                 width: `calc(100% - ${leftPanelWidth}px)`,
                 willChange: 'transform, width',
                 transform: 'translateX(0)',
                 transition: isResizing ? 'none' : 'all 0.2s cubic-bezier(0.4, 0, 0.2, 1)'
             }"
             :class="[
                'bg-white border-gray-200 bg-dots',
                'overflow-y-scroll'
             ]">
            <div>
                <DashboardComponent 
                    ref="dashboardRef"
                    @removeWidget="removeWidget"
                    v-if="reportLoaded && widgets"
                    :report="report" 
                    :edit="true" 
                    :widgets="widgets.filter(widget => widget.status === 'published')" 
                    :textWidgetsIds="textWidgetsIds"
                    @toggleSplitScreen="toggleSplitScreen"
                />
                <div v-else-if="reportLoaded && !widgets?.length" class="p-4 text-center text-gray-500">
                    No dashboard items yet.
                </div>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, nextTick, watch } from 'vue'

import GoBackChevron from '@/components/excel/GoBackChevron.vue';
import DashboardComponent from '~/components/DashboardComponent.vue';

const { signIn, signOut, token, data: currentUser, status, lastRefreshedAt, getSession } = useAuth()
const { organization, setOrganization } = useOrganization()
const { isExcel } = useExcel()
const route = useRoute()
const config = useRuntimeConfig()
const wsURL = config.public.wsURL
const reportLoaded = ref(false); // New loading state
const router = useRouter()


definePageMeta({ auth: true, layout: 'default' })

const subscription = computed(() => currentUser.value?.organizations[0]?.subscription)

const completions = ref([])
const isLoading = ref(false)
const report_id = route.params.id
const reportTitleInput = ref(null)
const isStoppingGeneration = ref(false)

const report = ref({
    title: '',
    id: '',
    slug: ''
});

const shareModalOpen = ref(false)

const sigkill = async (completion: any) => {
    isStoppingGeneration.value = true;
    try {
        await useMyFetch(`/api/completions/${completion.id}/sigkill`, {
            method: 'POST'
        });
        // After successful sigkill, update the local completion state
        const idx = completions.value.findIndex(c => c.id === completion.id);
        if (idx !== -1) {
            const prev = completions.value[idx];
            completions.value[idx] = {
                ...prev,
                status: 'stopped',
                sigkill: true,
                completion: { ...prev.completion }
            };
        } else {
            completion.status = 'stopped';
            completion.sigkill = true;
        }
    } catch (error) {
        console.error('Error updating sigkill:', error);
    } finally {
        isStoppingGeneration.value = false;
    }
}
const applyToExcel = (completion: any) => {
    // Serialize the entire completion object
    const serializedData = JSON.stringify(completion);

    console.log('Sending serialized data to Excel:', serializedData);
    window.parent.postMessage({
        type: 'applyToExcel',
        data: serializedData
    }, '*');
}

const excelData = ref({
    sheetName: '',
    address: '',
    sheetData: []
})

const promptValue = ref('')
const currentCompletion = ref('')
const isPageLoading = ref(true)
const widgets = ref([])
const mentions = ref([
    {
        name: 'MEMORY',
        items: []
    },
    {
        name: 'FILES',
        items: []
    },
    {
        name: 'DATA SOURCES',
        items: []
    },
]);

const latestInProgressCompletion = computed(() => {
    const inProgress = completions.value.filter((c: any) => c.role === 'system' && c.status === 'in_progress' && !c.sigkill);
    return inProgress.length ? inProgress[inProgress.length - 1] : null;
});

const rerunReport = () => {
    useMyFetch(`/api/reports/${report_id}/rerun`, {
        method: 'POST'
    }).then(() => {
        loadWidgets();
    })
}

function handleExcelMessage(event) {
    if (event.data.type === 'cellSelected') {
        // Update excelData reactively
        excelData.value = {
            sheetName: event.data.sheetName,
            address: event.data.address,
            sheetData: event.data.sheetData
        }
        console.log('Updated excelData:', excelData.value) // Add this line for debugging
    }
}

onMounted(() => {
    window.addEventListener('message', handleExcelMessage)
})

onUnmounted(() => {
    window.removeEventListener('message', handleExcelMessage)
})

function saveReportTitle() {
    const requestBody = {
        title: report.value.title
    };

    useMyFetch(`/api/reports/${report_id}`, {
        method: 'PUT',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(requestBody),
    }).then(() => {
        if (reportTitleInput.value) {
            reportTitleInput.value.blur();
        }
    });
}


async function loadReport() {
    const { data } = await useMyFetch(`/api/reports/${report_id}`);
    await nextTick();
    report.value = data.value;
    reportLoaded.value = true;
}

const selectedWidgetId = ref({ widgetId: null, stepId: null, widgetTitle: null });

// Function to handle the selected widget ID
function handleSelectedWidgetId(widgetId, stepId, widgetTitle) {
    selectedWidgetId.value = { widgetId, stepId, widgetTitle };
}

async function submitCompletion(promptValue) {
    const report_id = route.params.id
    const requestBody = {
        prompt: {
            content: promptValue.text,
            mentions: promptValue.mentions,
            widget_id: selectedWidgetId.value.widgetId,
            step_id: selectedWidgetId.value.stepId
        }
    }
    
    isLoading.value = true
    currentCompletion.value = ''

    // Add a new completion for the user's prompt
    completions.value.push({
        id: `user-${Date.now()}`,
        role: 'user',
        prompt: { content: promptValue.text }
    })

    const response = await useMyFetch(`/reports/${report_id}/completions.legacy`, {
        method: 'POST',
        body: JSON.stringify(requestBody),
        headers: {
            'Content-Type': 'application/json',
        },
    })
    // if first completion , send update title request
    if (completions.value.length == 1) {
    }
        if (response.error?.value?.data?.detail) {
            completions.value.push({
                id: `system-${Date.now()}`,
                role: 'system',
                completion: { content: response.error.value.data.detail  + " " + "Sign in to continue." },
                status: 'error'
            })
        }
    isLoading.value = false
    scrollToBottom()
}

async function loadWidgets() {
    try {
        const { data } = await useMyFetch(`/api/reports/${report_id}/widgets`);
        if (data.value) {
            widgets.value = data.value.map(widget => ({
                ...widget,
                key: Date.now() + widget.id + String(Math.random())
            }));
            // check if widget is published and if in not split screen, set isSplitScreen to true
            if (widgets.value.filter(widget => widget.status === 'published').length > 0 && !isSplitScreen.value) {
                toggleSplitScreen();
            }
            await nextTick();
        }
    } catch (error) {
        console.error('Error loading widgets:', error);
    }
}

async function updateCompletion(updated: any) {
  const index = completions.value.findIndex(c => c.id === updated.id);
  if (index === -1) return;

  // Check if the current completion has a widget_id property
  const currentWidgetId = completions.value[index]?.widget_id;
  const updatedWidgetId = updated?.widget_id;

  // Check if the current completion has a step_id property
  const currentStepId = completions.value[index]?.step_id;
  const updatedStepId = updated?.step_id;

  // Reload completions if widget_id or step_id is newly added
  if ((!currentWidgetId && updatedWidgetId) || (!currentStepId && updatedStepId)) {
    loadCompletions();
    return;
  }
  // Update in place
  completions.value[index] = {
    ...completions.value[index],
    completion: {
      ...completions.value[index].completion,
      content: updated.completion?.content || '',
      reasoning: updated.completion?.reasoning || '',
    },
    status: updated.status || '',
    sigkill: updated.sigkill || false
  };
}

async function loadCompletions() {
    const { data } = await useMyFetch(`/api/reports/${report_id}/completions.legacy`)
    completions.value = data.value
    isPageLoading.value = false
    scrollToBottom()
}
const textWidgetsIds = ref([])

function connectWebSocket() {

    const ws = new WebSocket(`${wsURL}/reports/${report_id}`)

    ws.onopen = () => {
        //console.log('WebSocket connection opened');
    };

    ws.onmessage = (event) => {
        //console.log('Received message:', event.data);
        if (event.data === "ping") {
            return;
        }
        const data = JSON.parse(event.data);
        const newCompletion = ref({})
        const role = ref('system')
        switch (data.event) {
            case "insert_completion":
                if (data.role == 'user') {
                    role.value = 'system'
                    return
                } else if (data.role == 'system') {
                    role.value = 'system'
                    if (data.status == 'error') {
                        loadCompletions();
                        return
                    }
                } else {
                    role.value = 'ai_agent'
                }
                newCompletion.value = {
                    id: data.id,
                    role: role.value, 
                    status: data.status,
                    completion: { content: data.completion.content || "", reasoning: data.completion.reasoning || "" }
                }
                // if last completion id is prefix system, dont add
                if (completions.value.length > 0 && completions.value[completions.value.length - 1].id.startsWith("system-") && completions.value[completions.value.length - 1].completion.content.length == 0) {
                    return;
                }
                completions.value.push(newCompletion.value)
                if (completions.value.length == 2) {
                    setTimeout(() => {
                        loadReport().then(() => {
                            shouldAnimateTitle.value = true;
                            // Reset the animation flag after animation completes
                            setTimeout(() => {
                                shouldAnimateTitle.value = false;
                            }, 600); // Match animation duration
                        });
                    }, 15000);
                }

                
                break;
            case 'update_completion':
                //loadCompletions();
                updateCompletion(data)
                break;
            case 'update_widget':
                loadWidgets()
                break;
            case 'insert_text_widget':
                if (dashboardRef.value) {
                    dashboardRef.value.refreshTextWidgets();
                } else {
                    console.warn("Dashboard component ref not available to refresh text widgets.");
                }
                break;
            case 'update_step':
                updateStep(data)
                //console.log('update_step', data)
                //loadWidgets()

                break;
            default:
                //console.log('Unknown event type:', data.event);
        }
        if (data.widget_id) {
            // Implement getWidget function or remove this if not needed
            // getWidget(data.widget_id);
        }
        //loadCompletions();
    };

    ws.onclose = () => {
        console.log('WebSocket connection closed');
    };

    ws.onerror = (error) => {
        console.error('WebSocket error:', error);
    // Add more detailed error handling here if needed
    }
}

const agentLogContainer = ref(null)
const scrollAnchor = ref(null)

function updateStep(updatedStep: any) {
  // 1) Prefer exact step match
  let idx = completions.value.findIndex((c: any) =>
    c.step_id === updatedStep.step_id || c.step?.id === updatedStep.step_id
  );

  // 2) If not found yet, pick the MOST RECENT system completion for this widget
  if (idx === -1) {
    for (let i = completions.value.length - 1; i >= 0; i--) {
      const c = completions.value[i];
      if (c.role === 'system' && c.widget_id === updatedStep.widget_id) {
        idx = i;
        break;
      }
    }
  }

  if (idx === -1) return;

  const current = completions.value[idx];
  const statusChanged = current?.step?.status !== updatedStep.status;

  const newCompletion = {
    ...current,
    widget_id: current.widget_id || updatedStep.widget_id,
    step: {
      id: updatedStep.step_id,
      title: updatedStep.title,
      slug: updatedStep.slug,
      status: updatedStep.status,
      code: updatedStep.code,
      data: updatedStep.data,
      data_model: updatedStep.data_model,
      type: updatedStep.type,
      description: updatedStep.description,
      widget_id: updatedStep.widget_id
    },
    step_id: updatedStep.step_id,
    _updateKey: statusChanged ? Date.now() : (current._updateKey || 0)
  };

  completions.value.splice(idx, 1, newCompletion);
}

function scrollToBottom() {
  // Wait for two tick cycles to ensure all content is rendered
    nextTick(() => {

        setTimeout(() => {
            const container = agentLogContainer.value
            if (container) {
                // Force layout recalculation
                container.offsetHeight
                // Scroll to the maximum possible position
                const scrollHeight = container.scrollHeight
                container.scrollTop = scrollHeight + 1000 // Add extra padding to ensure we reach bottom
            }
        }, 50)
    })
}


onMounted(async () => {
    await nextTick();
    await loadReport();
    await loadWidgets();
    await loadCompletions();
    if (route.query.new_message && completions.value.length == 0) {
        submitCompletion({ text: route.query.new_message as string, mentions: [] })
    }
    connectWebSocket();
    scrollToBottom();
});

watch(completions, (newCompletions, oldCompletions) => {
  scrollToBottom()
}, { deep: true })

const isSplitScreen = ref(false)

function toggleSplitScreen() {
    nextTick(() => {
        isSplitScreen.value = !isSplitScreen.value;
        if (isSplitScreen.value) {
            leftPanelWidth.value = 450;
        }
        scrollToBottom();
    });
}

const selectedWidget = ref(null);

async function removeWidget(widget: any) {
    await useMyFetch(`/api/reports/${report_id}/widgets/${widget.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status: 'draft', id: widget.id })
    }).then(() => {
        widgets.value = widgets.value.map(w => {
            if (w.id === widget.id) {
                return { ...w, status: 'draft' };
            }
            return w;
        });
    })
}

async function handleAddWidget(widget: any) {
    selectedWidget.value = widget;
    
    if (!isSplitScreen.value) {
        isSplitScreen.value = true;
        await nextTick();
    }

    if (!isExcel.value) {
        try {
            const { data: completeWidgetData } = await useMyFetch(`/api/reports/${report_id}/widgets/${widget.id}`);
            
            await useMyFetch(`/api/reports/${report_id}/widgets/${widget.id}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    status: 'published', 
                    id: widget.id,
                })
            });

            await loadWidgets();
            
        } catch (error) {
            console.error('Error updating widget:', error);
        }
    }
}

const leftPanelWidth = ref(450)
const isResizing = ref(false)
const initialMouseX = ref(0)
const initialPanelWidth = ref(0)

function startResize(e: MouseEvent) {
    isResizing.value = true
    initialMouseX.value = e.clientX
    initialPanelWidth.value = leftPanelWidth.value
    
    document.addEventListener('mousemove', handleResize)
    document.addEventListener('mouseup', stopResize)
    document.body.style.userSelect = 'none'
}

function handleResize(e: MouseEvent) {
    if (!isResizing.value) return
    
    const minWidth = 280
    const maxWidth = window.innerWidth * 0.8
    
    // Calculate the distance moved from initial position
    const dx = e.clientX - initialMouseX.value
    const newWidth = initialPanelWidth.value + dx
    
    // Apply constraints
    leftPanelWidth.value = Math.min(Math.max(newWidth, minWidth), maxWidth)
}

function stopResize() {
    isResizing.value = false
    document.removeEventListener('mousemove', handleResize)
    document.removeEventListener('mouseup', stopResize)
    document.body.style.userSelect = 'auto'
}

const createNewReport = async () => {
    const response = await useMyFetch('/reports', {
        method: 'POST',
        body: JSON.stringify({title: 'untitled report',
         files: []})
    });

    if (!response.code === 200) {
        throw new Error('Report creation failed');
    }

    const data = await response.data.value;
    router.push({
        path: `/reports/${data.id}`
    })
}

onUnmounted(() => {
    document.removeEventListener('mousemove', handleResize)
    document.removeEventListener('mouseup', stopResize)
    document.body.style.userSelect = 'auto'
})

// Add new ref for controlling animation
const shouldAnimateTitle = ref(false)

// Add ref for PromptBoxExcel component
const promptBoxRef = ref(null);

// Add ref for DashboardComponent
const dashboardRef = ref(null);

// Add function to handle example click
function handleExampleClick(starter: string) {
    if (promptBoxRef.value) {
        promptBoxRef.value.updatePromptContent(starter);
    }
}

</script>

<style scoped>
.bg-dots {
    background-image: radial-gradient(circle, rgba(0, 0, 0, 0.15) 1px, #fff 1px);
    background-size: 20px 20px; /* Adjust the size of the dots */
}

.overflow-y-auto {
    overflow-y: auto !important;
    max-height: calc(100vh - 200px);
}

/* Add this to handle the animation of the right panel appearing/disappearing */
.v-enter-active,
.v-leave-active {
    transition: opacity 0.3s ease, transform 0.3s ease;
}

.v-enter-from,
.v-leave-to {
    opacity: 0;
    transform: translateX(20px);
}

.cursor-col-resize {
    cursor: col-resize;
}

/* Prevent text selection while resizing */
.user-select-none {
    user-select: none;
}

.fade-in {
    animation: fadeIn 0.6s ease-in;
}

@keyframes fadeIn {
    0% {
        opacity: 0;
        transform: translateY(10px);
    }
    100% {
        opacity: 1;
        transform: translateY(0);
    }
}

@keyframes fade-in {
    0% {
        opacity: 0;
        transform: translateY(-2px);
    }
    100% {
        opacity: 1;
        transform: translateY(0);
    }
}

.animate-fade-in {
    animation: fade-in 0.6s ease-in;
}

/* Add smooth transition for split screen */
.flex-row {
    transform-style: preserve-3d; /* Hardware acceleration */
    backface-visibility: hidden; /* Reduce visual artifacts */
    perspective: 1000px; /* 3D acceleration */
}

/* Add hardware acceleration to resizable elements */
[style*="transition"] {
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    transform-style: preserve-3d;
    backface-visibility: hidden;
    perspective: 1000px;
}
</style>
