<template>
    <div class="container mx-auto flex flex-col h-full">
    <div class="w-full bg-[#fff] p-2 flex justify-between text-sm sticky top-0 z-50 border-b-2 border-gray-200">
        <div class="flex items-center gap-4">
            <div class="space-x-2 flex items-center" v-if="widget">
                <UTooltip text="Collapse">
                    <button @click="$emit('toggleSplitScreen')" class="text-xs items-center flex gap-1 hover:bg-gray-100 px-0 py-1 rounded">
                        <Icon name="heroicons:chevron-double-right" />
                    </button>
                </UTooltip>
                <Icon name="heroicons:view-columns"/>
                &nbsp;&nbsp;
                {{ widget?.title }}
            </div>
        </div>
    </div>
    <div v-if="widget" class="w-full flex-1 bg-white">
        <div class="flex items-center text-sm">
                <div id="widget-controls" class="w-1/2 absolute right-0 top-2 z-10 text-right" v-show="widget.showControls">
                    <div class="mr-2 inline-flex gap-2">
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
                <RenderCount :widget="widget" :data="widget.last_step?.data" :data_model="widget.last_step?.data_model" />
            </div>
            <div v-if="widget.last_step?.data_model?.type == 'table'" class="h-[100vh]">
                <AgGridComponent ref="agGrid" height="100vh" :columnDefs="widget.last_step?.data?.columns"
                    :rowData="widget.last_step?.data?.rows" class="h-full" v-if="!widget.show_data_model && !widget.show_data" />
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
    </div>
</template>

<script setup lang="ts">


const props = defineProps<{
    widget: any
}>()

function toggleDataModel(widget: any) {
    console.log(widget)
}

function toggleData(widget: any) {
    console.log(widget)
}
</script>
