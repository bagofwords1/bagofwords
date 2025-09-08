<template>
    <div class="flex pl-2 md:pl-4 text-sm min-h-screen" v-if="report.title !== ''" :style="pageStyle">
        <div class="w-full px-4 pl-0">
            <div class="container mx-auto">
                <a v-if="report.general?.bow_credit !== false" href="https://bagofwords.com" target="_blank" class="fixed z-[1000] bottom-5 right-5 block bg-black text-gray-200 font-light px-2 py-1 rounded-md text-xs">
                    Made with <span class="font-bold text-white">Bag of words</span>
                </a>
                <div class="p-2 pl-5">
                    <div>
                        <h1 class="text-xl font-semibold mt-4" :style="titleStyle">{{ report.title }}

                            <button @click="copyToClipboard(`/r/${report_id}`)" class="hover:opacity-70 relative" :style="{ color: 'inherit' }">
                                <Icon name="heroicons:link" class="w-4 h-4" />
                                <span v-if="showCopied"
                                    class="absolute -top-8 left-1/2 transform -translate-x-1/2 bg-gray-800 text-white text-sm px-2 py-1 rounded shadow-lg">
                                    Copied!
                                </span>
                            </button>
                        </h1>
                        <span class="text-sm" :style="{ color: subtitleColor }">
                            {{ report.user.name }}

                        </span>

                    </div>
                    <div class="pt-10">
                        <DashboardComponent :widgets="displayedWidgets" :report="report" :edit="false" />
                    </div>
                </div>
            </div>
        </div>
    </div>
</template>

<script setup>
import DashboardComponent from '~/components/DashboardComponent.vue';
import { useDashboardTheme } from '~/components/dashboard/composables/useDashboardTheme';

const route = useRoute();
const report_id = route.params.id;
const displayedTextWidgets = ref([]);

const report = ref({
    title: '',
    id: '',
    slug: '',
    user: {
        name: ''
    },
    created_at: '',
    status: ''
});

// computed for widgets
const displayedWidgets = computed(() => widgets.value) ;


const widgets = ref([]);

const showCopied = ref(false);

// Theme application for the page
const themeName = computed(() => report.value?.report_theme_name || report.value?.theme_name || 'default');
const themeOverrides = computed(() => report.value?.theme_overrides || {});
const { tokens } = useDashboardTheme(themeName, themeOverrides, null);

const pageStyle = computed(() => ({
    backgroundColor: tokens.value?.background || '#ffffff',
    color: tokens.value?.textColor || '#0f172a',
    fontFamily: tokens.value?.fontFamily || 'inherit'
}));

const titleStyle = computed(() => ({
    color: tokens.value?.textColor || '#0f172a',
    fontFamily: tokens.value?.headingFontFamily || tokens.value?.fontFamily || 'inherit'
}));

const subtitleColor = computed(() => {
    const baseColor = tokens.value?.textColor || '#6b7280';
    // Make subtitle slightly more muted than the main text
    if (baseColor.startsWith('#')) {
        // For hex colors, add some opacity
        return baseColor + '99'; // 60% opacity
    }
    return baseColor;
});

definePageMeta({
    layout: false,
    auth: false

})

async function loadReport() {
    const { data } = await useMyFetch(`/api/r/${report_id}`);
    if (!data.value) {
        navigateTo('/not_found');
    }
    report.value = data.value;
}

const getWidgets = async () => {
    const { data } = await useMyFetch(`/api/r/${report_id}/widgets`);
    widgets.value = data.value;
}

const copyToClipboard = async (path) => {
    const fullUrl = window.location.origin + path;
    try {
        await navigator.clipboard.writeText(fullUrl);
        showCopied.value = true;
        setTimeout(() => {
            showCopied.value = false;
        }, 2000);
    } catch (err) {
        console.error('Failed to copy URL:', err);
    }
};

onMounted(async () => {
    nextTick(async () => {
        loadReport();
        await getWidgets();
    })
})

</script>
