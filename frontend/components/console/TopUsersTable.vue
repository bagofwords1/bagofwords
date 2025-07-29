<template>
    <div class="bg-white rounded-2xl border border-gray-100 shadow-sm overflow-hidden">
        <div class="p-6 border-b border-gray-50">
            <h3 class="text-lg font-semibold text-gray-900">Top Users</h3>
            <p class="text-sm text-gray-500 mt-1">Most active users this period</p>
        </div>
        <div class="p-0">
            <div v-if="isLoading" class="flex items-center justify-center h-40">
                <div class="flex items-center space-x-2">
                    <div class="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600"></div>
                    <span class="text-gray-600">Loading users...</span>
                </div>
            </div>
            <div v-else class="overflow-hidden">
                <table class="min-w-full">
                    <thead class="bg-gray-50">
                        <tr>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">User</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Messages</th>
                            <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Trend</th>
                        </tr>
                    </thead>
                    <tbody class="bg-white divide-y divide-gray-200">
                        <tr v-for="(user, index) in topUsersData?.top_users || []" :key="user.user_id" class="hover:bg-gray-50">
                            <td class="px-6 py-4 whitespace-nowrap">
                                <div class="flex items-center">
                                    <div class="flex-shrink-0 h-8 w-8">
                                        <div class="h-8 w-8 rounded-full bg-gradient-to-r from-blue-400 to-purple-500 flex items-center justify-center">
                                            <span class="text-white text-sm font-medium">{{ user.name.charAt(0) }}</span>
                                        </div>
                                    </div>
                                    <div class="ml-4">
                                        <div class="text-sm font-medium text-gray-900">{{ user.name }}</div>
                                        <div class="text-sm text-gray-500">{{ user.role || 'User' }}</div>
                                    </div>
                                </div>
                            </td>
                            <td class="px-6 py-4 whitespace-nowrap">
                                <div class="text-sm font-semibold text-gray-900">{{ user.messages_count }}</div>
                            </td>
                            <td class="px-6 py-4 whitespace-nowrap">
                                <span :class="[
                                    'inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium',
                                    user.trend_percentage > 0 ? 'bg-green-100 text-green-800' : 
                                    user.trend_percentage < 0 ? 'bg-red-100 text-red-800' : 
                                    'bg-gray-100 text-gray-800'
                                ]">
                                    {{ user.trend_percentage > 0 ? '+' : '' }}{{ user.trend_percentage }}%
                                </span>
                            </td>
                        </tr>
                        <tr v-if="!topUsersData?.top_users?.length" class="hover:bg-gray-50">
                            <td colspan="3" class="px-6 py-4 text-center text-gray-500">
                                No user data available for this period
                            </td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
interface TopUserData {
    user_id: string
    name: string
    email?: string
    role?: string
    messages_count: number
    queries_count: number
    trend_percentage: number
}

interface DateRange {
    start: string
    end: string
}

interface TopUsersMetrics {
    top_users: TopUserData[]
    total_users_analyzed: number
    date_range: DateRange
}

interface Props {
    topUsersData: TopUsersMetrics | null
    isLoading: boolean
}

defineProps<Props>()
</script>