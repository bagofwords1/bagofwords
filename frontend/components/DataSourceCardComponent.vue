<template>
    <div class="data-card-component">
        <div class="data-card-component__header mb-2 px-3 markdown-wrapper">
            <MDC :value="truncatedDescription" class="markdown-content" />
            <button 
                v-if="description.length > 140" 
                @click="isExpanded = !isExpanded"
                class="text-blue-500 text-xs mb-1"
            >
                {{ isExpanded ? 'Show less' : 'Read more' }}
            </button>
        </div>
        <div class="flex border-b border-gray-200 mb-2">
            <button @click="activeTab = 'tables'" class="px-4 py-1 text-xs"
                :class="{ 'border-b-2 border-blue-500 text-blue-600': activeTab === 'tables', 'text-gray-500': activeTab !== 'tables' }">
                Tables
            </button>
        </div>

        <div v-if="isLoading" class="flex justify-center items-center p-4">
            <Icon name="heroicons-arrow-path" class="animate-spin" />
        </div>
        <div v-else>
            <div v-if="activeTab === 'tables'">

                <div v-if="schema" class="mt-2">
                    <ul class="py-2 list-none list-inside">
                        <li class="py-1" v-for="table in schema" :key="table.name">
                            <div @click="toggleTable(table)" class="font-semibold text-gray-500 cursor-pointer">
                                <Icon :name="expandedTables[table.name] ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="p-1" />
                                {{ table.name }}
                            </div>
                            <ul v-if="expandedTables[table.name]" class="ml-4 mt-1 text-xs">
                                <li v-for="column in table.columns" :key="column.name" class="flex py-0.5">
                                    <span class="text-gray-500 mr-2">{{ column.name }}</span>
                                    <span class="text-gray-400">{{ column.dtype }}</span>
                                </li>
                            </ul>
                        </li>
                    </ul>
                </div>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
const props = defineProps<{
    data_source: any
}>()

const isLoading = ref(true)
const schema = ref(null)
const expandedTables = ref<Record<string, boolean>>({})
const activeTab = ref('tables')
const isExpanded = ref(false)
const description = computed(() => props.data_source.description || "Example description: This is a description of the data source.")
const truncatedDescription = computed(() => {
    if (isExpanded.value || description.value.length <= 140) {
        return description.value
    }
    return description.value.slice(0, 140) + '...'
})

// Remove await and handle the fetch in onMounted
onMounted(async () => {
    const { data, error } = await useMyFetch(`/api/data_sources/${props.data_source.id}/schema`)
    schema.value = data.value
    isLoading.value = false
})

const toggleTable = (table: any) => {
    expandedTables.value[table.name] = !expandedTables.value[table.name]
}
</script>

<style scoped>
.cursor-pointer {
    cursor: pointer;
}


.markdown-wrapper :deep(.markdown-content) {
    /* Basic text styling */
    @apply text-gray-700 leading-relaxed;
    font-size: 12px;

    /* Headers */
    :where(h1, h2, h3, h4, h5, h6) {
        @apply font-bold mb-4 mt-6;
    }

    h1 {
        @apply text-3xl;
    }

    h2 {
        @apply text-2xl;
    }

    h3 {
        @apply text-xl;
    }

    /* Lists */
    ul,
    ol {
        @apply pl-6 mb-4;
    }

    ul {
        @apply list-disc;
    }

    ol {
        @apply list-decimal;
    }

    li {
        @apply mb-1.5;
    }

    /* Code blocks */
    pre {
        @apply bg-gray-50 p-4 rounded-lg mb-4 overflow-x-auto;
    }

    code {
        @apply bg-gray-50 px-1 py-0.5 rounded text-sm font-mono;
    }

    /* Links */
    a {
        @apply text-blue-600 hover:text-blue-800 underline;
    }

    /* Block quotes */
    blockquote {
        @apply border-l-4 border-gray-200 pl-4 italic my-4;
    }

    /* Tables */
    table {
        @apply w-full border-collapse mb-4;

        th,
        td {
            @apply border border-gray-200 p-2;
            @apply text-xs;
            @apply p-1.5;
            @apply bg-white;
        }

        th {
            @apply bg-gray-50;
            @apply p-1.5;
            @apply text-xs;
        }
    }

    /* Paragraphs and spacing */
    p {
        @apply mb-1;
    }

    /* Images */
    img {
        @apply max-w-full h-auto rounded-lg;
    }
}
</style>