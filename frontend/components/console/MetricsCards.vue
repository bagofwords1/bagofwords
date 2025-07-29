<template>
    <div class="grid grid-cols-1 md:grid-cols-6 gap-6 mb-8">
        <!-- Messages -->
        <div class="bg-white p-6 border border-gray-200 rounded-xl shadow-sm">
            <div class="text-2xl font-bold text-gray-900">
                {{ metricsComparison?.current.total_messages || 0 }}
            </div>
            <div class="text-sm font-medium text-gray-600 mt-1">Messages</div>
            <div v-if="metricsComparison?.changes.total_messages" class="text-xs mt-2">
                <span :class="getChangeClass(metricsComparison.changes.total_messages.percentage)">
                    {{ formatChange(metricsComparison.changes.total_messages) }}
                </span>
            </div>
        </div>
        
        <!-- Queries -->
        <div class="bg-white p-6 border border-gray-200 rounded-xl shadow-sm">
            <div class="text-2xl font-bold text-gray-900">
                {{ metricsComparison?.current.total_queries || 0 }}
            </div>
            <div class="text-sm font-medium text-gray-600 mt-1">Queries</div>
            <div v-if="metricsComparison?.changes.total_queries" class="text-xs mt-2">
                <span :class="getChangeClass(metricsComparison.changes.total_queries.percentage)">
                    {{ formatChange(metricsComparison.changes.total_queries) }}
                </span>
            </div>
        </div>
        
        <!-- Accuracy -->
        <div class="bg-white p-6 border border-gray-200 rounded-xl shadow-sm">
            <div class="text-2xl font-bold text-gray-900">
                {{ metricsComparison?.current.accuracy || 'N/A' }}
            </div>
            <div class="text-sm font-medium text-gray-600 mt-1">Accuracy</div>
        </div>
        
        <!-- Instructions Coverage -->
        <div class="bg-white p-6 border border-gray-200 rounded-xl shadow-sm">
            <div class="text-2xl font-bold text-gray-900">
                {{ metricsComparison?.current.instructions_coverage || 'N/A' }}
            </div>
            <div class="text-sm font-medium text-gray-600 mt-1 flex items-center">
                Instructions Coverage
                <UTooltip text="Percentage of user queries that have matching instruction templates">
                    <Icon name="heroicons-information-circle" class="w-4 h-4 ml-1 text-gray-400 cursor-help" />
                </UTooltip>
            </div>
        </div>
        
        <!-- Feedbacks -->
        <div class="bg-white p-6 border border-gray-200 rounded-xl shadow-sm">
            <div class="text-2xl font-bold text-gray-900">
                {{ metricsComparison?.current.total_feedbacks || 0 }}
            </div>
            <div class="text-sm font-medium text-gray-600 mt-1">Feedbacks</div>
            <div v-if="metricsComparison?.changes.total_feedbacks" class="text-xs mt-2">
                <span :class="getChangeClass(metricsComparison.changes.total_feedbacks.percentage)">
                    {{ formatChange(metricsComparison.changes.total_feedbacks) }}
                </span>
            </div>
        </div>
        
        <!-- Active Users -->
        <div class="bg-white p-6 border border-gray-200 rounded-xl shadow-sm">
            <div class="text-2xl font-bold text-gray-900">
                {{ metricsComparison?.current.active_users || 0 }}
            </div>
            <div class="text-sm font-medium text-gray-600 mt-1">Active Users</div>
            <div v-if="metricsComparison?.changes.active_users" class="text-xs mt-2">
                <span :class="getChangeClass(metricsComparison.changes.active_users.percentage)">
                    {{ formatChange(metricsComparison.changes.active_users) }}
                </span>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
interface MetricChange {
    absolute: number
    percentage: number
}

interface SimpleMetrics {
    total_messages: number
    total_queries: number
    total_feedbacks: number
    accuracy: string
    instructions_coverage: string
    active_users: number
}

interface MetricsComparison {
    current: SimpleMetrics
    previous: SimpleMetrics
    changes: Record<string, MetricChange>
    period_days: number
}

interface Props {
    metricsComparison: MetricsComparison | null
}

defineProps<Props>()

const formatChange = (change: MetricChange) => {
    const sign = change.percentage > 0 ? '+' : ''
    return `${sign}${change.percentage.toFixed(1)}% (${sign}${change.absolute})`
}

const getChangeClass = (percentage: number) => {
    if (percentage > 0) return 'text-green-600'
    if (percentage < 0) return 'text-red-600'
    return 'text-gray-500'
}
</script> 