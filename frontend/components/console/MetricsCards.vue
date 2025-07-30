<template>
    <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-6 gap-6 mb-8">
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
            <div v-if="metricsComparison?.changes.accuracy" class="text-xs mt-2">
                <span :class="getChangeClass(metricsComparison.changes.accuracy.percentage)">
                    {{ formatChange(metricsComparison.changes.accuracy) }}
                </span>
            </div>
        </div>
        
        <!-- Instructions Effectiveness -->
        <div class="bg-white p-6 border border-gray-200 rounded-xl shadow-sm">
            <div class="text-2xl font-bold text-gray-900">
                {{ Math.round(metricsComparison?.current.instructions_effectiveness) }}%
            </div>
            <div class="text-sm font-medium text-gray-600 mt-1 flex items-center">
                Instructions Effectiveness
                <UTooltip text="AI judge score for how well instructions guide responses (20-100 scale)">
                    <Icon name="heroicons-information-circle" class="w-4 h-4 ml-1 text-gray-400 cursor-help" />
                </UTooltip>
            </div>
            <div v-if="metricsComparison?.changes.instructions_effectiveness" class="text-xs mt-2">
                <span :class="getChangeClass(metricsComparison.changes.instructions_effectiveness.percentage)">
                    {{ formatJudgeChange(metricsComparison.changes.instructions_effectiveness) }}
                </span>
            </div>
        </div>
        
        <!-- Context Effectiveness -->
        <div class="bg-white p-6 border hidden border-gray-200 rounded-xl shadow-sm">
            <div class="text-2xl font-bold text-gray-900">
                {{ formatScore(metricsComparison?.current.context_effectiveness) }}
            </div>
            <div class="text-sm font-medium text-gray-600 mt-1 flex items-center">
                Context Effectiveness
                <UTooltip text="AI judge score for context quality and relevance (0-100)">
                    <Icon name="heroicons-information-circle" class="w-4 h-4 ml-1 text-gray-400 cursor-help" />
                </UTooltip>
            </div>
            <div v-if="metricsComparison?.changes.context_effectiveness" class="text-xs mt-2">
                <span :class="getChangeClass(metricsComparison.changes.context_effectiveness.percentage)">
                    {{ formatJudgeChange(metricsComparison.changes.context_effectiveness) }}
                </span>
            </div>
        </div>
        
        <!-- Response Quality -->
        <div class="bg-white p-6 border hidden border-gray-200 rounded-xl shadow-sm">
            <div class="text-2xl font-bold text-gray-900">
                {{ formatScore(metricsComparison?.current.response_quality) }}
            </div>
            <div class="text-sm font-medium text-gray-600 mt-1 flex items-center">
                Response Quality
                <UTooltip text="AI judge score for overall response quality (0-100)">
                    <Icon name="heroicons-information-circle" class="w-4 h-4 ml-1 text-gray-400 cursor-help" />
                </UTooltip>
            </div>
            <div v-if="metricsComparison?.changes.response_quality" class="text-xs mt-2">
                <span :class="getChangeClass(metricsComparison.changes.response_quality.percentage)">
                    {{ formatJudgeChange(metricsComparison.changes.response_quality) }}
                </span>
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
    instructions_effectiveness: number
    context_effectiveness: number
    response_quality: number
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

const formatJudgeChange = (change: MetricChange) => {
    const sign = change.percentage > 0 ? '+' : ''
    return `${sign}${change.percentage.toFixed(1)}% (${sign}${change.absolute.toFixed(1)})`
}

const formatScore = (score: number | undefined) => {
    if (score === undefined || score === null) return 'N/A'
    return score.toFixed(1)
}

const getChangeClass = (percentage: number) => {
    if (percentage > 0) return 'text-green-600'
    if (percentage < 0) return 'text-red-600'
    return 'text-gray-500'
}
</script> 