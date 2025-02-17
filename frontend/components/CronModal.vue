<template>
    <UTooltip text="Schedule or rerun report">
        <button @click="cronModalOpen = true"
            class="text-lg items-center flex gap-1 hover:bg-gray-100 px-2 py-1 rounded">
            <Icon name="heroicons:clock" />
        </button>
    </UTooltip>


    <UModal v-model="cronModalOpen">
        <div class="p-4 relative">
            <button @click="cronModalOpen = false"
                class="absolute top-2 right-2 text-gray-500 hover:text-gray-700 outline-none">
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            <h1 class="text-lg font-semibold">Schedule and rerun report</h1>
            <p class="text-sm text-gray-500">Schedule this report to run on a regular basis</p>
            <hr class="my-4" />
            <div>
                <div class="mt-4">
                    <label class="block text-sm font-medium text-gray-700 mb-2">Schedule Frequency</label>
                    <select v-model="selectedSchedule" class="w-full rounded-md border border-gray-200 px-3 py-2 text-sm">
                        <option v-for="option in cronOptions" :key="option.value" :value="option.value">
                            {{ option.label }}
                        </option>
                    </select>
                </div>
                
            </div>

            <div class="border-t border-gray-200 pt-4 mt-8">
                <div class="flex justify-end space-x-2">
                    <button 
                        @click="cronModalOpen = false"
                        class="px-3 py-1.5 text-xs font-medium text-gray-700 bg-white border border-gray-300 rounded-md hover:bg-gray-50"
                    >
                        Cancel
                    </button>
                    <button 
                        @click="scheduleReport"
                        class="px-3 py-1.5 text-xs font-medium text-white bg-blue-500 border border-transparent rounded-md hover:bg-blue-600"
                    >
                        Schedule
                    </button>
                </div>
            </div>
        </div>
    </UModal>
</template>

<script lang="ts" setup>
const cronModalOpen = ref(false);
const toast = useToast();
const props = defineProps<{
    report: any
}>();

const report = ref(props.report);

const reportUrl = computed(() => `${window.location.origin}/r/${report.value.id}`);
// set report to published

const showTooltip = ref(false);

const selectedSchedule = ref(props.report.cron_schedule || 'None');

const cronOptions = ref([
    { value: 'None', label: 'None' },
    //{ value: '*/5 * * * * *', label: 'Every 5 Seconds' },
    { value: '*/15 * * * *', label: 'Every 15 Minutes' },
    { value: '0 * * * *', label: 'Hourly' },
    { value: '0 0 * * *', label: 'Daily (Midnight)' },
    { value: '0 0 * * 1', label: 'Weekly (Monday Midnight)' },
]);

async function scheduleReport() {
    const response = await useMyFetch(`/api/reports/${report.value.id}/schedule`, {
        method: 'POST',
        query: {
            cron_expression: selectedSchedule.value,
        }
    });
    if (response.data.value) {
        toast.add({
            title: 'Report scheduled',
            color: 'green',
            description: 'Report scheduled successfully',
        });
    }
    else {
        toast.add({
            title: 'Error',
            color: 'red',
            description: response.data.value.message,
        });
    }
}


</script>