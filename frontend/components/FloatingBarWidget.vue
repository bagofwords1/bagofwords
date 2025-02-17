<template>
        <div class="glow">
        </div>

        <div class="w-full z-50 h-14 float-color absolute bottom-0 left-1/2 transform -translate-x-1/2 flex items-center justify-start space-x-4 pl-4 text-xs">
            <div class="flex space-x-2 font-bold text-sm mr-4">
                /
            </div>
            <USelectMenu
                v-model="selectedWidget"
                :options="menuOptions"
                option-attribute="title"
                @change="changeSelect"
                class="w-3/5"
                searchable
                :uiMenu="{
                    option: {
                        base: 'text-xs',
                    },
                }"
            >
                <template #option="{ option }">
                    <span v-if="option.id !== 'dashboard'" class="flex items-center space-x-2">
                        <Icon name="heroicons-chart-bar" v-if="option.status == 'success' && option.last_step.type === 'chart'" />
                        <Icon name="heroicons-table-cells" v-if="option.status == 'success' && option.last_step.type === 'table'" />
                        <span>{{ option.title }}</span>
                    </span>
                    <span v-else class="flex items-center space-x-2">
                        <Icon name="heroicons-chart-pie" />
                        <span class="text-blue-400">Dashboard</span>
                    </span>
                </template>

                <template #label>
                    <Icon name="heroicons-chart-pie" v-if="selectedWidget.id === 'dashboard'" />
                    <Icon name="heroicons-chart-bar" v-else-if="selectedWidget.status == 'success' && selectedWidget.last_step.type === 'chart'" />
                    <Icon name="heroicons-table-cells" v-else-if="selectedWidget.status == 'success' && selectedWidget.last_step.type === 'table'" />
                    <span class="text-xs">{{ selectedWidget.title }}</span>
                </template>
            </USelectMenu>


            <button @click="toggleChat" class="text-sm p-2 rounded-lg mr-2 hover:bg-white border border-gray-200  text-xs flex items-center space-x-2">
                <Icon name="heroicons-chat-bubble-left-right" /> 
                <span>New chat</span>
            </button>
            <button @click="" class="text-xs p-2 rounded-lg mr-2 hidden hover:bg-gray-100">
            Excel | CSV
        </button>

    </div>
    
</template>

<script setup lang="ts">
    import { ref, watch, onMounted, computed } from 'vue';

    const props = defineProps({
        widgets: {
            type: Array,
            required: true
        },
        toggleChat: {
            type: Function,
            required: true
        }
    });

    const dashboardOption = {
        title: 'Dashboard',
        icon: 'heroicons-home',
        id: 'dashboard',
        selected: true,
        status: 'success',
    };

    const menuOptions = computed(() => [dashboardOption, ...props.widgets]);

    const selectedWidget = ref(dashboardOption);

    watch(() => props.widgets, (newVal) => {
        const selectedWidgetFromProps = newVal.find((widget: any) => widget.selected === true);
        selectedWidget.value = selectedWidgetFromProps || dashboardOption;
    }, { deep: true });

    function changeSelect(widget: any) {
        if (widget.id === 'dashboard') {
            props.widgets.forEach((w: any) => w.selected = false);
            // Emit event to notify index.vue that no widget is selected
            window.dispatchEvent(new CustomEvent('widget-selected', { detail: null }));
            props.toggleChat(); // Call toggleChat as a prop
        } else {
            props.widgets.forEach((w: any) => w.selected = false);
            widget.selected = true;
            // Emit event to notify index.vue
            window.dispatchEvent(new CustomEvent('widget-selected', { detail: widget }));
        }
        selectedWidget.value = widget;
    }

    // Add event listener when the component is mounted
    onMounted(() => {
        window.addEventListener('widget-selected', (event: any) => {
            const widget = event.detail;
            selectedWidget.value = widget;
        });
    });

</script>

<style scoped>
.glow {
    position: fixed;
    left: 50%; /* Center horizontally */
    top: 99%;  /* Center vertically */
    transform: translate(-50%, -50%);
    width: 230px;
    height: 140px;
    opacity: 0.2; /* Make it visible */
    transition: all 1s ease;
    pointer-events: none;
    border-radius: 9999px;
    background-image: linear-gradient(45deg, rgb(96 165 250), #8699e8, hwb(184 48% 6%));
    filter: blur(60px);
    z-index: 49;
}

.float-color {
    background: linear-gradient(to bottom right, #333, #444);
    background:#f8f8f8;
    border-top: 2px solid #ccc;
}
</style>