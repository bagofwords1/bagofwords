<template>
    <UTooltip text="Publish">
        <button @click="publishModalOpen = true"
            class="text-sm items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded border border-gray-200 bg-cyan-100 text-cyan-700">
            <Icon name="heroicons:globe-alt" />
            <span class="text-sm">Publish</span>
        </button>
    </UTooltip>


    <UModal v-model="publishModalOpen">
        <div class="p-4 relative">
            <button @click="publishModalOpen = false"
                class="absolute top-2 right-2 text-gray-500 hover:text-gray-700 outline-none">
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            <h1 class="text-lg font-semibold">Publish Dashboard</h1>
            <p class="text-sm text-gray-500">Make this dashboard publicly accessible</p>
            <hr class="my-4" />
            <div class="flex flex-row items-center text-sm">
                Allow public access to this dashboard
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
                <div v-if="isPublished" class="mt-4 font-normal">
                    <a :href="reportUrl" target="_blank" class="text-blue-500 hover:underline">
                        <Icon name="heroicons:arrow-top-right-on-square" />
                        View dashboard</a>
                </div>
            </div>
            <div class="border-t border-gray-200 pt-4 mt-8">
                <button @click="publishModalOpen = false"
                    class="bg-gray-50 border border-gray-200 rounded-md px-3 py-2 text-xs hover:bg-gray-100">Close</button>
            </div>
        </div>
    </UModal>
</template>

<script lang="ts" setup>
const publishModalOpen = ref(false);
const toast = useToast();
const props = defineProps<{
    report: any
}>();

const report = ref(props.report);
const reportUrl = computed(() => `${window.location.origin}/r/${report.value.id}`);

const isPublished = computed(() => report.value.status === 'published');

const publishReport = async (newValue: boolean) => {
    const response = await useMyFetch(`/api/reports/${props.report.id}/publish`, {
        method: 'POST',
    })
    if (response.status.value === 'success') {
        report.value.status = newValue ? 'published' : 'draft';
        toast.add({
            title: 'Dashboard published',
            description: `Your dashboard is now ${newValue ? 'public' : 'private'}`,
            color: 'green',
        })
    }
    else {
        toast.add({
            title: 'Error',
            description: 'Failed to publish dashboard',
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
    }, 2000);
}

</script>

