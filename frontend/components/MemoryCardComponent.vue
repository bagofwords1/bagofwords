<template>
    <div class="data-card-component">
        <div class="data-card-component__header mb-2 px-3">
            {{ memory.description }}
        </div>
    </div>
    <div class="text-left text-xs">
        <WidgetTabsComponent :widget="widget" :step="widget?.last_step" />
    </div>
</template>

<script setup lang="ts">
const props = defineProps<{
    memory: any
}>()
const widget = ref(null)

import WidgetTabsComponent from '~/components/WidgetTabsComponent.vue'



onMounted(async () => {
    const { data } = await useMyFetch(`/api/memories/${props.memory.id}/widget`)
    widget.value = data.value
})
</script>