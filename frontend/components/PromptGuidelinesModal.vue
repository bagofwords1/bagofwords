<template>
    <UModal v-model="promptGuidelinesModal" :ui="{ width: 'sm:max-w-3xl' }">
        <div class="p-6 relative">
            <button @click="promptGuidelinesModal = false"
                class="absolute top-2 right-2 text-gray-500 hover:text-gray-700 outline-none">
                <Icon name="heroicons:x-mark" class="w-5 h-5" />
            </button>
            <h1 class="text-lg font-semibold">Prompt Guidelines</h1>
            <p class="text-sm text-gray-500">How to get the best results from your prompts</p>
            <hr class="my-4" />
            <div class="space-y-4">
                <div class="space-y-2">
                    <h2 class="font-medium">Tips for Better Prompts:</h2>
                    <ul class="list-disc pl-5 space-y-1 text-sm">
                        <li>Be specific about what you want to visualize or analyze</li>
                        <li>Mention the type of visualization if you have a preference (table, bar chart, line chart, etc.)</li>
                        <li>Include time periods or specific metrics you're interested in</li>
                        <li>Ask follow-up questions to modify existing visualizations</li>
                    </ul>
                </div>

                <div class="space-y-2">
                    <h2 class="font-medium">Example Prompts:</h2>
                    <ul class="list-disc pl-5 space-y-1 text-sm">
                        <li>"Show me monthly sales trends in a line chart"</li>
                        <li>"Create a table of top 10 customers by revenue"</li>
                        <li>"Add a profit margin column to this table"</li>
                        <li>"Convert this table into a bar chart"</li>
                        <li>"Create a dashboard showing sales performance overview"</li>
                    </ul>
                </div>

                <div class="mt-4 p-3 bg-blue-50 rounded-md">
                    <p class="text-sm text-blue-700">
                        💡 Tip: You can always ask follow-up questions or request modifications to any visualization!
                    </p>
                </div>
            </div>
        </div>
    </UModal>
</template>

<script setup lang="ts">
    const promptGuidelinesModal = ref(false)

    const openModal = () => {
        promptGuidelinesModal.value = true
    }

    defineExpose({ openModal })
</script>