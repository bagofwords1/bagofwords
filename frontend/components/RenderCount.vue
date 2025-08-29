<template>
    <div class="pt-0 pl-2">
        <div v-if="show_title" class="font-bold text-gray-400">{{ widget.title }}</div>
        <div v-if="data?.info?.total_rows" class="text-xl font-bold mt-2">
            {{ countValue || "None" }}
        </div>
        <div v-else>
            Loading..
        </div>
    </div>
</template>

<script setup lang="ts">
import { ref, watch, toRefs } from 'vue';

const props = defineProps<{
    widget: any
    data: any
    data_model: any
    show_title: boolean
}>()

// Convert to refs
const { data, widget, show_title } = toRefs(props);

// Make count reactive with ref
const countValue = ref(null);

// Initial setup
const updateData = () => {
    if (data.value?.rows?.length > 0) {
        countValue.value = Object.values(data.value.rows[0])[0] 
    }
}

// Watch for changes
watch(data, updateData, { deep: true, immediate: true });
</script>