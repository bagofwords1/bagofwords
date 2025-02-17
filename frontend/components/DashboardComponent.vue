<template>
<div class="container mx-auto">
    <div v-if="props.edit" class="w-full bg-[#fff] p-2 flex justify-between text-sm sticky top-0 z-50 border-b-2 border-gray-200">
        <div class="flex items-center gap-4">
            <div class="space-x-2">
                <UTooltip text="Collapse">
                    <button @click="$emit('toggleSplitScreen')" class="text-xs items-center flex gap-1 hover:bg-gray-100 px-0 py-1 rounded">
                        <Icon name="heroicons:chevron-double-right" />
                    </button>
                </UTooltip>
            </div>
            <div class="font-medium text-gray-700">
                Dashboard 
            </div>
        </div>
        <div class="space-x-2 flex items-center">
            <UTooltip text="Add text element">
                <button v-if="props.edit" @click="showTextWidgetEditor = !showTextWidgetEditor"class="text-lg items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded">
                    <Icon name="heroicons:italic" />
                </button>
            </UTooltip>
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
                <button @click="isModalOpen = true" class="text-lg items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded">
                    <Icon name="heroicons:arrows-pointing-out" />
                </button>
            </UTooltip>
            <ShareModal :report="report" />
        </div>
    </div>
  <div class="overflow-x-hidden overflow-y-scroll overflow-x-scroll w-full h-full"> 

    
    <div id="canvas" 
        :class="{ 
            'relative': true, 
            'overflow-y-scroll': true, 
            'overflow-x-scroll': true, 
            'border-none border-gray-200': props.edit 
        }" 
        :style="{ 
            transform: `scale(${props.edit ? zoom * 1 : 1})`, 
            transformOrigin: 'left top', 
            minHeight: `${canvasHeight}px`,  
            width: '1200px' 
        }">

        <TransitionGroup name="fade">
            <vue-draggable-resizable
                v-for="textWidget in textWidgets"
            :parent="true"
            :grid="[40, 40]"
            :key="textWidget.id"
            :w="textWidget.width"
            :h="textWidget.height"
            :x="textWidget.x"
            :y="textWidget.y"
            :resizable="props.edit"
            :draggable="props.edit"
            @drag-stop="(left, top) => handleTextWidgetDrag(left, top, textWidget)"
            @resize-stop="(left, top, width, height) => handleTextWidgetResize(left, top, width, height, textWidget)"
            :class="[
                'border border-solid border-gray-200',
                'absolute',
                'bg-white'
            ]"
        >
        <div id="text-widget-controls" v-if="props.edit" class="absolute right-2 top-1 z-10 flex gap-2">
            <button class="text-xs items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded border" 
                    @click="removeTextWidget(textWidget)">
                <Icon name="heroicons:trash" />
            </button>
            <button class="text-xs items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded border" 
                    @click="textWidget.isEditing = !textWidget.isEditing">
                <Icon name="heroicons:pencil" v-if="!textWidget.isEditing"/>
                <Icon name="heroicons:x-mark" v-if="textWidget.isEditing"/>
            </button>
        </div>
        <div v-if="textWidget.isEditing">
            <TextWidgetEditor :textWidget="textWidget"
                @save="(content) => saveTextWidgetEdit(content, textWidget)"
                @cancel="textWidget.isEditing = false"
            />
        </div>
        <div v-else class="w-full h-full p-2 text-black rendered-html" v-html="textWidget.content"></div>
            </vue-draggable-resizable>
        </TransitionGroup>


        <TransitionGroup name="fade">
            <vue-draggable-resizable
                v-for="widget in displayedWidgets"
                :parent="true"
                :grid="[40, 40]"
                :key="widget.id"
                :w="widget.width"
        :h="widget.height"
        :x="widget.x"
        :y="widget.y"
        :resizable="props.edit"
        :draggable="props.edit"
        @drag-stop="(left, top) => handleWidgetDrag(left, top, widget)"
        @resize-stop="(left, top, width, height) => handleWidgetResize(left, top, width, height, widget)"
        @mouseenter="widget.showControls = true"
        @mouseleave="widget.showControls = false"
        :class="[
            'border border-solid border-gray-200',
            'absolute',
            'bg-white'
        ]"
    >
        <div class="w-full h-full relative overflow-scroll pt-3" >
            <div class="flex items-center text-sm py-3 px-2">
                <div class="w-full absolute left-2 top-3">
                    <div>
                        <span v-if="widget.last_step?.type == 'table' || widget.last_step === null">{{ widget.title }}</span>
                        <span v-if="widget.last_step?.data?.loadingColumn" class="text-gray-400">Loading...</span>
                    </div>
                </div>
                <div id="widget-controls" class="w-1/2 absolute right-0 top-2 z-10 text-right" v-show="widget.showControls">
                    <div class="mr-2 inline-flex gap-2">
                        <button v-if="props.edit" class="text-xs items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded border" @click="removeWidget(widget)">
                            <Icon name="heroicons:trash" /> Remove
                        </button>
                        <button @click="toggleDataModel(widget)" class="hidden text-xs items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded border">
                            <Icon name="heroicons:table-cells" />
                            Data Model
                        </button>
                        <button @click="toggleData(widget)" class="hidden text-xs items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded border">
                            <Icon name="heroicons-table-cells" />
                            Data
                        </button>
                    </div>
                </div>
            </div>
            <div v-if="widget.last_step?.data_model?.type == 'count'" class="mt-2">
                <RenderCount :show_title="false" :widget="widget" :data="widget.last_step?.data" :data_model="widget.last_step?.data_model" />
            </div>
            <div v-if="widget.last_step?.data_model?.type == 'table'" class="mt-2 h-full">
                <AgGridComponent ref="agGrid" :columnDefs="widget.last_step?.data?.columns"
                    :rowData="widget.last_step?.data?.rows" class="px-2" v-if="!widget.show_data_model && !widget.show_data" />
                <div v-if="widget.show_data_model">
                    {{ widget.last_step?.data_model }}
                </div>
                <div v-if="widget.show_data">
                    {{ widget.last_step?.data }}
                </div>
            </div>
            <div v-if="widget.last_step?.data_model?.type?.includes('chart')" class="z-10">
                <RenderVisual 
                 v-if="!widget.show_data_model && !widget.show_data"
                :widget="widget" :data="widget.last_step?.data" :data_model="widget.last_step?.data_model" />
                <div v-if="widget.show_data_model">
                    {{ widget.last_step?.data_model }}
                </div>
                <div v-if="widget.show_data">
                    {{ widget.last_step?.data }}
                </div>
            </div>
            <div v-if="widget.last_step?.type == 'init'" class="text-center items-center flex flex-col pt-14">
                <SpinnerComponent />
                Loading..
            </div>
            </div>
            </vue-draggable-resizable>
        </TransitionGroup>

        <TransitionGroup name="fade">
            <vue-draggable-resizable
                v-if="showTextWidgetEditor"
            :parent="true"
            :grid="[40, 40]"
            :w="editorSize.width"
            :h="editorSize.height"
            :x="editorPosition.x"
            :y="editorPosition.y"
            :resizable="true"
            :draggable="true"
            @drag-stop="(left, top) => { editorPosition = { x: left, y: top }}"
            @resize-stop="(left, top, width, height) => { 
                editorPosition = { x: left, y: top };
                editorSize = { width, height };
            }"
            :class="[
                'rounded-lg',
                'absolute',
                'bg-white',
                'shadow-lg',
                'z-40'
            ]"
        >
            <div class="w-full h-full relative">
                <div class="flex flex-col h-full">
                    <TextWidgetEditor 
                        @save="saveTextWidget"
                        :initial-content="hello"
                        @cancel="showTextWidgetEditor = false"
                    />
                </div>
            </div>
            </vue-draggable-resizable>
        </TransitionGroup>

</div>

<div v-if="props.edit" class="zoom-controls fixed bottom-4 right-4 bg-white rounded-lg shadow-lg p-2 flex gap-2 z-50">

    <button @click="zoomIn" class="p-2 hover:bg-gray-100 rounded">
        <Icon name="heroicons:plus-circle" />
    </button>
    <button @click="zoomOut" class="p-2 hover:bg-gray-100 rounded">
        <Icon name="heroicons:minus-circle" />
    </button>
    <button @click="resetZoom" class="p-2 hover:bg-gray-100 rounded">
        <Icon name="heroicons:arrows-pointing-in" />
    </button>
</div>
</div>

<Teleport to="body">
    <div v-if="isModalOpen || isModalMounted" 
         class="fixed inset-0 bg-black bg-opacity-50 z-50 flex items-center justify-center transition-opacity duration-300"
         :class="{ 'opacity-0': !isModalMounted, 'opacity-100': isModalMounted }"
         @click="closeModal">
        <div class="bg-white w-[95vw] h-[95vh] rounded-lg flex flex-col transition-transform duration-300"
             :class="{ 'scale-95 opacity-0': !isModalMounted, 'scale-100 opacity-100': isModalMounted }"
             @click.stop>
            <!-- Modal Header -->
            <div class="p-2 flex justify-end items-center">
                <button @click="isModalOpen = false" class="text-gray-500 hover:text-gray-700">
                    <Icon name="heroicons:x-mark" class="w-5 h-5" />
                </button>
            </div>
            
            <!-- Modal Content -->
            <div class="flex-1 overflow-auto p-4">
                <div id="modal-canvas" class="mx-auto z-[1000]" :style="{ transform: `scale(${zoom})`, transformOrigin: 'center top', zIndex: 1000 }">
                    <!-- Copy of the canvas content -->
                     <template v-for="textWidget in textWidgets" :key="textWidget.id">
                        <div
                        :style="{
                            position: 'absolute',
                            left: `${textWidget.x}px`,
                            top: `${textWidget.y}px`,
                            width: `${textWidget.width}px`,
                            height: `${textWidget.height}px`
                        }" class="border border-solid border-gray-200 bg-white">
                            <div class="w-full h-full p-2 text-black rendered-html" v-html="textWidget.content"></div>
                        </div>
                    </template>

                    <template v-for="widget in displayedWidgets" :key="widget.id">
                        <div :style="{
                            position: 'absolute',
                            left: `${widget.x}px`,
                            top: `${widget.y}px`,
                            width: `${widget.width}px`,
                            height: `${widget.height}px`
                        }" class="border border-solid border-gray-200 bg-white">
                            <div class="w-full h-full relative overflow-scroll pt-3">
                                <!-- Widget content (same as original) -->
                                <div class="flex items-center text-sm py-3 px-2">
                                    <div class="w-full absolute left-2 top-3">
                                        <span v-if="widget.last_step?.type == 'table' || widget.last_step === null">{{ widget.title }}</span>
                                        <span v-if="widget.last_step?.data?.loadingColumn" class="text-gray-400">Loading...</span>
                                    </div>
                                </div>
                                <div>
                                    <div v-if="widget.last_step?.data_model?.type == 'count'" class="mt-2">
                                        <RenderCount :show_title="false" :widget="widget" :data="widget.last_step?.data" :data_model="widget.last_step?.data_model" />
                                    </div>
                                    <div v-if="widget.last_step?.data_model?.type == 'table'" class="mt-2 h-full">
                                        <AgGridComponent ref="agGrid" :columnDefs="widget.last_step?.data?.columns"
                                            :rowData="widget.last_step?.data?.rows" class="px-2 h-full" v-if="!widget.show_data_model && !widget.show_data" />
                                        <div v-if="widget.show_data_model">
                                            {{ widget.last_step?.data_model }}
                                        </div>
                                        <div v-if="widget.show_data">
                                            {{ widget.last_step?.data }}
                                        </div>
                                    </div>
                                    <div v-if="widget.last_step?.data_model?.type?.includes('chart')" class="z-10">
                                        <RenderVisual :widget="widget" :data="widget.last_step?.data" :data_model="widget.last_step?.data_model" />
                                    </div>
                                    <div v-if="widget.last_step?.type == 'init'" class="text-center items-center flex flex-col pt-14">
                                        <SpinnerComponent />
                                        Loading..
                                    </div>
                                </div>
                            </div>
                        </div>
                    </template>
                </div>
            </div>
        </div>
    </div>
</Teleport>

</div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, nextTick, watch } from 'vue';

import { useMyFetch } from '~/composables/useMyFetch';

import RenderVisual from './RenderVisual.vue';

const toast = useToast();

const emit = defineEmits(['removeWidget', 'addTextWidget', 'removeTextWidget', 'toggleSplitScreen']);

const props = defineProps<{
    report: any
    edit: boolean
    widgets: any[]
    textWidgetsIds: string[] | undefined
}>();

const textWidgets = ref([]);

const displayedWidgets = computed(() => props.widgets || []);

const zoom = ref(1)
const zoomStep = 0.1
const minZoom = 0.1
const maxZoom = 1.2

const zoomIn = () => {
    if (zoom.value < maxZoom) {
        zoom.value = Math.min(zoom.value + zoomStep, maxZoom)
    }
}

const zoomOut = () => {
    if (zoom.value > minZoom) {
        zoom.value = Math.max(zoom.value - zoomStep, minZoom)
    }
}

const resetZoom = () => {
    zoom.value = 1
}

// Add wheel zoom support
const handleWheel = (event: WheelEvent) => {
    if (event.ctrlKey) {
        event.preventDefault()
        if (event.deltaY < 0) {
            zoomIn()
        } else {
            zoomOut()
        }
    }
}

const canvasHeight = ref(1400)

// Add this new function to calculate canvas height
const updateCanvasHeight = () => {
    const padding = 20; // bottom padding
    const maxY = displayedWidgets.value.reduce((max, widget) => {
        const bottomY = widget.y + widget.height;
        return bottomY > max ? bottomY : max;
    }, 0);

    // Calculate the full height based on the viewport size
    const fullHeight = window.innerHeight - padding;

    // Set the canvas height to the full height or the calculated maxY, whichever is larger, but not exceeding a maximum height
    const maxHeight = 2000; // Set your desired maximum height here
    canvasHeight.value = Math.min(Math.max(800, maxY + padding, fullHeight), maxHeight);
};

const handleWidgetDrag = async (left: number, top: number, widget: any) => {
    widget.x = left;
    widget.y = top;
    updateCanvasHeight();
    const requestBody = { id: widget.id, x: widget.x, y: widget.y, width: widget.width, height: widget.height };

    try {
        await useMyFetch(`/api/reports/${props.report.id}/widgets/${widget.id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestBody),
        });
    } catch (error) {
        console.error(`Failed to update widget ${widget.id} position`, error);
    }
};

const handleWidgetResize = async (left: number, top: number, width: number, height: number, widget: any) => {
    widget.x = left;
    widget.y = top;
    widget.width = width;
    widget.height = height;
    updateCanvasHeight();
    const requestBody = { id: widget.id, x: widget.x, y: widget.y, width: widget.width, height: widget.height };

    try {
        await useMyFetch(`/api/reports/${props.report.id}/widgets/${widget.id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestBody),
        });
    } catch (error) {
        console.error(`Failed to update widget ${widget.id} dimensions`, error);
    }
};

const removeWidget = (widget: any) => {
    emit('removeWidget', widget);
};



const showTextWidgetEditor = ref(false);

async function getTextWidgets() {
    try {
        let url = '';
        if (props.report.status === 'published' && props.edit === false) {
            url = `/api/r/${props.report.id}/text_widgets`;
        } else {
            url = `/api/reports/${props.report.id}/text_widgets`;
        }
        const { data } = await useMyFetch(url, {
            method: 'GET',
        });
        
        // Ensure data.value exists and is an array
        const widgetsArray = Array.isArray(data.value) ? data.value : [];
        
        // Force a re-render by creating a new array
        textWidgets.value = widgetsArray.map((widget: any) => ({
            ...widget,
            isEditing: false,
            key: `${widget.id}-${Date.now()}` // Add a unique key to force re-render
        }));
        
        // Force canvas update
        await nextTick();
        updateCanvasHeight();
    } catch (error) {
        console.error('Failed to fetch text widgets:', error);
        textWidgets.value = []; // Reset to empty array on error
    }
}

async function removeTextWidget(widget: any) {
    const response = await useMyFetch(`/api/reports/${props.report.id}/text_widgets/${widget.id}`, {
        method: 'DELETE',
    })
    // remove the widget from the textWidgets array
    textWidgets.value = textWidgets.value.filter((textWidget: any) => textWidget.id !== widget.id);
}

const saveTextWidget = async (content: string) => {
    try {
        await useMyFetch(`/api/reports/${props.report.id}/text_widgets`, {
            method: 'POST',
            body: JSON.stringify({ 
                content,
                x: editorPosition.value.x,
                y: editorPosition.value.y,
                width: editorSize.value.width,
                height: editorSize.value.height
            })
        });
        
        await getTextWidgets();
        showTextWidgetEditor.value = false;
        // Reset position and size for next time
        editorPosition.value = { x: 0, y: 0 };
        editorSize.value = { width: 400, height: 140 };
    } catch (error) {
        console.error('Failed to save text widget', error);
    }
};

const editTextWidget = (widget: any) => {
    widget.isEditing = true;
};

const saveTextWidgetEdit = async (content: string, widget: any) => {
    try {
        await useMyFetch(`/api/reports/${props.report.id}/text_widgets/${widget.id}`, {
            method: 'PUT',
            body: JSON.stringify({ 
                id: widget.id,
                content: content,
                x: editorPosition.value.x,
                y: editorPosition.value.y,
                width: editorSize.value.width,
                height: editorSize.value.height
            })
        });
        
        widget.content = content;
        widget.isEditing = false;
        await getTextWidgets();
    } catch (error) {
        console.error('Failed to update text widget', error);
    }
};

const handleTextWidgetDrag = async (left: number, top: number, widget: any) => {
    widget.x = left;
    widget.y = top;
    updateCanvasHeight();
    const requestBody = { id: widget.id, x: widget.x, y: widget.y };

    try {
        await useMyFetch(`/api/reports/${props.report.id}/text_widgets/${widget.id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestBody),
        });
    } catch (error) {
        console.error(`Failed to update text widget ${widget.id} position`, error);
    }
};

const handleTextWidgetResize = async (left: number, top: number, width: number, height: number, widget: any) => {
    widget.x = left;
    widget.y = top;
    widget.width = width;
    widget.height = height;
    updateCanvasHeight();
    const requestBody = { id: widget.id, x: widget.x, y: widget.y, width: widget.width, height: widget.height };

    try {
        await useMyFetch(`/api/reports/${props.report.id}/text_widgets/${widget.id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestBody),
        });
    } catch (error) {
        console.error(`Failed to update text widget ${widget.id} dimensions`, error);
    }
};

async function rerunReport() {
    console.log('rerun report');
    const response = await useMyFetch(`/api/reports/${props.report.id}/rerun`, {
        method: 'POST',
    });
    if (response.data.value) {
        toast.add({
            title: 'Rerunning report',
            description: response.data.value.message,
        });
    }
    else {
        toast.add({
            title: 'Error',
            description: response.data.value.message,
            color: 'red'
        });
    }
}




// Update the watch to be immediate and handle empty arrays
watch(
    [() => props.textWidgetsIds, () => props.edit], 
    async ([newIds, newEdit], [oldIds, oldEdit]) => {
        // Always fetch on component initialization or when IDs/edit mode changes
        await getTextWidgets();
    }, 
    { immediate: true, deep: true }  // Add immediate: true to run on component creation
);

onMounted(async () => {
    nextTick(async () => {
        document.getElementById('canvas')?.addEventListener('wheel', handleWheel, { passive: false });
        await getTextWidgets();
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && isModalOpen.value) {
                closeModal();
            }
        });
    });
});

onBeforeUnmount(() => {
    document.getElementById('canvas')?.removeEventListener('wheel', handleWheel)
    document.removeEventListener('keydown', (e) => {
        if (e.key === 'Escape' && isModalOpen.value) {
            closeModal();
        }
    });
});

// Add these refs to store the current editor position and size
const editorPosition = ref({ x: 0, y: 0 });
const editorSize = ref({ width: 400, height: 140 });

// Add this new function to handle fullscreen
const toggleFullscreen = () => {
    const canvas = document.getElementById('canvas');
    if (!canvas) return;

    if (!document.fullscreenElement) {
        canvas.requestFullscreen().catch((err) => {
            console.error(`Error attempting to enable fullscreen: ${err.message}`);
        });
    } else {
        document.exitFullscreen();
    }
};

const isModalOpen = ref(false);
const isModalMounted = ref(false);

// Update the existing isModalOpen watch or create one
watch(isModalOpen, (newValue) => {
    if (newValue) {
        // Small delay to trigger the animation
        nextTick(() => {
            isModalMounted.value = true;
        });
    } else {
        isModalMounted.value = false;
    }
});

const closeModal = () => {
    isModalMounted.value = false;
    setTimeout(() => {
        isModalOpen.value = false;
    }, 300); // Match this duration with the transition duration
};

const toggleDataModel = (widget: any) => {
    widget.show_data_model = !widget.show_data_model;
    if (widget.show_data_model) {
        widget.show_data = false;
    }
};

const toggleData = (widget: any) => {
    widget.show_data = !widget.show_data;
    if (widget.show_data) {
        widget.show_data_model = false;
    }
};

</script>

<style scoped>

.vue-flow {
    height: 100%;
}

.vue-flow__container {
    height: 100%;
}

.custom-node {
    padding: 10px;
    border-radius: 5px;
    background: white;
    border: 1px solid #ddd;
}

#canvas {
    transition: transform 0.2s ease-out;
}

.zoom-controls {
    z-index: 1000 !important;
}

.rendered-html {
    font-size: 14px;
    line-height: 1.5;

    :deep(h1) {
        font-size: 1.2rem;
        font-weight: 600;
        margin: 1rem 0;
    }

    :deep(h2) {
        font-size: 1rem;
        font-weight: 600;
        margin: 0.8rem 0;
    }

    :deep(p) {
        margin: 0.5rem 0;
    }

    :deep(a) {
        color: #3b82f6;
        text-decoration: underline;
    }

    :deep(strong) {
        font-weight: 600;
    }

    :deep(em) {
        font-style: italic;
    }
}

.rendered-html p {
    margin-top: 10px;
    margin-bottom: 10px;
}

/* Add these new styles */
#canvas:fullscreen {
    background-color: white;
    padding: 20px;
    overflow: auto !important;
}

.widget-transition {
  transition: all 0.3s ease-out;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.3s ease, transform 0.3s ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
  transform: translateY(20px) scale(0.95);
}

/* Add these new styles to ensure proper transition behavior */
.fade-move {
  transition: transform 0.3s ease;
}

.fade-leave-active {
  position: absolute;
}
</style>
