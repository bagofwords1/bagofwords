<template>
    <UTooltip text="Share">
        <button @click="shareModalOpen = true"
            class="text-sm items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded border border-gray-200 bg-cyan-100 text-cyan-700">
            <Icon name="heroicons:arrow-down-tray" />
            <span class="text-sm">Share</span>
        </button>
    </UTooltip>


    <UModal v-model="shareModalOpen">
        <div class="p-4 relative">
            <button @click="shareModalOpen = false"
                class="absolute top-2 right-2 text-gray-500 hover:text-gray-700 outline-none">
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            <h1 class="text-lg font-semibold">Share</h1>
            <p class="text-sm text-gray-500">Share this report with others</p>
            <hr class="my-4" />
            <div class="flex flex-row items-center text-sm">
                Allow public access to this report
                <UToggle color="sky" :model-value="isPublished" class="ml-2" @update:model-value="publishReport" />
            </div>
            <div class="flex flex-col mt-4 text-sm" v-if="isPublished">
                <div class="my-2 font-semibold">URL</div>
                <div class="flex">
                    <input :value="reportUrl" type="text" class="py-2 px-2 border border-gray-200 rounded-md w-[95%]"
                        disabled />
                    <button @click="copyReportUrl"
                        class="ml-2 bg-gray-50 border border-gray-200 rounded-md px-3 text-xs hover:bg-gray-100 relative">
                        Copy
                        <span v-if="showTooltip"
                            class="absolute top-full left-1/2 transform -translate-x-1/2 mt-1 bg-black text-white text-xs rounded py-1 px-2">
                            Copied!
                        </span>
                    </button>
                </div>
            </div>
            <div class="border-t border-gray-200 pt-4 mt-8">
                <button @click="shareModalOpen = false"
                    class="bg-gray-50 border border-gray-200 rounded-md px-3 py-2 text-xs hover:bg-gray-100">Close</button>
            </div>
        </div>
    </UModal>
</template>

<script lang="ts" setup>
const shareModalOpen = ref(false);
const toast = useToast();
const props = defineProps<{
    report: any
}>();

const report = ref(props.report);
const reportUrl = computed(() => `${window.location.origin}/r/${report.value.id}`);
// set report to published

const isPublished = computed(() => report.value.status === 'published');

const publishReport = async (newValue: boolean) => {
    const response = await useMyFetch(`/api/reports/${props.report.id}/publish`, {
        method: 'POST',
    })
    if (response.status.value === 'success') {

        report.value.status = newValue ? 'published' : 'draft';
        toast.add({
            title: 'Report published',
            description: `Your report is now ${newValue ? 'public' : 'private'}`,
            color: 'green',
        })
    }
    else {
        toast.add({
            title: 'Error',
            description: 'Failed to publish report',
            color: 'red',
        })
    }
}

const showTooltip = ref(false);

const copyReportUrl = () => {
    navigator.clipboard.writeText(reportUrl.value);
    showTooltip.value = true;
    setTimeout(() => {
        showTooltip.value = false;
    }, 2000); // Tooltip will disappear after 2 seconds
}

</script>