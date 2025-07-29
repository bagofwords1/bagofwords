<template>
    <div class="p-6">
        <div class="mb-6">
            <h1 class="text-2xl font-bold text-gray-900">Diagnosis</h1>
            <p class="text-gray-600 mt-1">Detailed analysis of system activity and performance</p>
        </div>

        <!-- Recent Widgets Table -->
        <div class="bg-white rounded-2xl border border-gray-100 shadow-sm overflow-hidden">
            <div class="p-6 border-b border-gray-50">
                <h2 class="text-lg font-semibold text-gray-900">Recent Activity</h2>
                <p class="text-sm text-gray-500 mt-1">Latest widgets and user interactions</p>
            </div>
            
            <!-- Loading state -->
            <div v-if="isLoading" class="flex items-center justify-center py-8">
                <div class="flex items-center space-x-2">
                    <div class="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600"></div>
                    <span class="text-gray-600">Loading widgets...</span>
                </div>
            </div>
            
            <!-- Table -->
            <div v-else class="overflow-x-auto">
                <table class="min-w-full divide-y divide-gray-200">
                    <thead class="bg-gray-50">
                        <tr>
                            <th class="px-3 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider w-1/6">Widget</th>
                            <th class="px-3 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider w-1/12">User</th>
                            <th class="px-3 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider w-1/12">Time</th>
                            <th class="px-3 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider w-1/6">Completion ID</th>
                            <th class="px-3 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider w-1/6">Row Count</th>
                            <th class="px-3 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider w-1/12">Revisions</th>
                            <th class="px-3 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider w-1/12">Feedback</th>
                        </tr>
                    </thead>
                    <tbody class="bg-white divide-y divide-gray-200">
                        <tr v-for="widget in recentWidgets" :key="widget.id" class="hover:bg-gray-50">
                            <td class="px-3 py-4 cursor-pointer" @click="openModal(widget)">
                                <div class="text-sm font-medium text-gray-900 truncate" :title="widget.title">
                                    {{ widget.title }}
                                </div>
                            </td>
                            <td class="px-3 py-4 text-sm text-gray-500 truncate cursor-pointer" :title="widget.user_name" @click="openModal(widget)">
                                {{ widget.user_name }}
                            </td>
                            <td class="px-3 py-4 text-sm text-gray-500 cursor-pointer" @click="openModal(widget)">
                                {{ formatDate(widget.created_at) }}
                            </td>
                            <td class="px-3 py-4 text-sm text-gray-500 cursor-pointer" @click="openModal(widget)">
                                <div class="truncate max-w-32">
                                    {{ widget.completion_id || 'N/A' }}
                                </div>
                            </td>
                            <td class="px-3 py-4 text-sm text-gray-500 cursor-pointer" @click="openModal(widget)">
                                {{ widget.row_count || 0 }}
                            </td>
                            <td class="px-3 py-4 text-sm text-gray-500 text-center cursor-pointer" @click="openModal(widget)">
                                {{ widget.steps_count }}
                            </td>
                            <td class="px-3 py-4 text-sm text-gray-500">
                                <div class="flex items-center gap-1" @click.stop>
                                    <span class="text-center min-w-[20px]">{{ widget.thumbs_count }}</span>
                                    <UButton
                                        :icon="widget.feedback_summary?.user_feedback?.direction === 1 ? 'i-heroicons-hand-thumb-up-solid' : 'i-heroicons-hand-thumb-up'"
                                        :color="widget.feedback_summary?.user_feedback?.direction === 1 ? 'black' : 'gray'"
                                        variant="ghost"
                                        size="xs"
                                        @click="sendFeedback(widget.completion_id, 1)"
                                        :disabled="!widget.completion_id"
                                        :loading="feedbackLoading[widget.completion_id || '']"
                                    />
                                    <UButton
                                        :icon="widget.feedback_summary?.user_feedback?.direction === -1 ? 'i-heroicons-hand-thumb-down-solid' : 'i-heroicons-hand-thumb-down'"
                                        :color="widget.feedback_summary?.user_feedback?.direction === -1 ? 'black' : 'gray'"
                                        variant="ghost"
                                        size="xs"
                                        @click="handleNegativeFeedback(widget.completion_id, widget.feedback_summary?.user_feedback)"
                                        :disabled="!widget.completion_id"
                                        :loading="feedbackLoading[widget.completion_id || '']"
                                    />
                                </div>
                            </td>
                        </tr>
                    </tbody>
                </table>
            </div>
            
            <!-- Pagination Controls -->
            <div v-if="totalItems > 0" class="px-6 py-4 border-t border-gray-100">
                <div class="flex items-center justify-between">
                    <div class="text-sm text-gray-700">
                        Showing {{ (currentPage - 1) * pageSize + 1 }} to {{ Math.min(currentPage * pageSize, totalItems) }} of {{ totalItems }} results
                    </div>
                    <div class="flex items-center space-x-2">
                        <UButton
                            icon="i-heroicons-chevron-left"
                            color="gray"
                            variant="ghost"
                            size="sm"
                            @click="handlePageChange(currentPage - 1)"
                            :disabled="!hasPrevPage"
                        >
                            Previous
                        </UButton>
                        
                        <!-- Page numbers -->
                        <div class="flex items-center space-x-1">
                            <template v-for="page in getVisiblePages()" :key="page">
                                <UButton
                                    v-if="typeof page === 'number'"
                                    :color="page === currentPage ? 'blue' : 'gray'"
                                    :variant="page === currentPage ? 'solid' : 'ghost'"
                                    size="sm"
                                    @click="handlePageChange(page)"
                                    class="min-w-[32px]"
                                >
                                    {{ page }}
                                </UButton>
                                <span v-else class="px-2 text-gray-500">...</span>
                            </template>
                        </div>
                        
                        <UButton
                            icon="i-heroicons-chevron-right"
                            color="gray"
                            variant="ghost"
                            size="sm"
                            @click="handlePageChange(currentPage + 1)"
                            :disabled="!hasNextPage"
                        >
                            Next
                        </UButton>
                    </div>
                </div>
            </div>
        </div>

        <!-- Move your existing modal and negative feedback modal code here too -->
    </div>
</template>

<script setup lang="ts">
// Move all the widgets table logic from ConsoleOverview.vue here
// This includes:
// - All widget-related interfaces
// - recentWidgets state
// - pagination state and logic
// - modal state and functions
// - feedback functions
// - fetchWidgets function

definePageMeta({
    layout: 'console'
})

// Add all the logic here...
</script>