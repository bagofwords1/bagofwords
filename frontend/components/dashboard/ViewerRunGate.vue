<template>
    <!-- Viewer gate for withheld dashboards: the shared snapshot is per-user
         (user-scoped sources or RLS), so this viewer sees their own data or a
         state telling them the one thing that unblocks them. Normally the page
         auto-runs and only 'loading' is ever visible; the other states are the
         fallback when auto-run can't succeed. -->
    <div class="text-center max-w-sm mx-auto px-6">
        <!-- Loading: auto-run (or a retry) in flight -->
        <template v-if="state === 'loading'">
            <Spinner class="w-10 h-10 mx-auto mb-3 text-gray-400 text-[40px]" />
            <h3 class="text-base font-semibold text-gray-900 dark:text-white mb-1">
                Loading your data
            </h3>
            <p class="text-sm text-gray-500 dark:text-gray-400">
                Running this dashboard's queries for you...
            </p>
        </template>

        <!-- Anonymous: identity is required to resolve their data. The source's
             brand icon says which account the sign-in unlocks. -->
        <template v-else-if="state === 'signin'">
            <DataSourceIcon v-if="sourceType" :type="sourceType" class="h-10 mx-auto mb-3" />
            <Icon v-else name="heroicons:user-circle" class="w-10 h-10 mx-auto mb-3 text-gray-400" />
            <h3 class="text-base font-semibold text-gray-900 dark:text-white mb-1">
                Sign in to see your data
            </h3>
            <p class="text-sm text-gray-500 dark:text-gray-400 mb-4">
                This dashboard shows data based on your access.
            </p>
            <a :href="`/users/sign-in?redirect=/r/${reportId}`"
                class="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700">
                Sign in
            </a>
        </template>

        <!-- The viewer's own connection is missing (e.g. Power BI not linked) -->
        <template v-else-if="state === 'connect'">
            <Icon name="heroicons:link" class="w-10 h-10 mx-auto mb-3 text-gray-400" />
            <h3 class="text-base font-semibold text-gray-900 dark:text-white mb-1">
                Connect {{ sourceNames }} to see your data
            </h3>
            <p class="text-sm text-gray-500 dark:text-gray-400 mb-4">
                This dashboard loads data from {{ sourceNames }} using your account.
            </p>
            <div class="flex items-center justify-center gap-2">
                <a v-if="connectSourceId" :href="`/integrations/${connectSourceId}/connection`"
                    class="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700">
                    Connect
                </a>
                <button @click="$emit('run')" :disabled="isRunning"
                    class="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-200 border border-gray-200 dark:border-gray-700 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 disabled:opacity-50">
                    Try again
                </button>
            </div>
        </template>

        <!-- The viewer has no permission on the data source: nothing they can
             self-serve — name the source and point at an admin. -->
        <template v-else-if="state === 'no_access'">
            <Icon name="heroicons:lock-closed" class="w-10 h-10 mx-auto mb-3 text-gray-400" />
            <h3 class="text-base font-semibold text-gray-900 dark:text-white mb-1">
                You don't have access to this dashboard's data
            </h3>
            <p class="text-sm text-gray-500 dark:text-gray-400">
                Ask an admin for access to {{ sourceNames }}.
            </p>
        </template>

        <!-- The run failed for another reason -->
        <template v-else-if="state === 'error'">
            <Icon name="heroicons:exclamation-triangle" class="w-10 h-10 mx-auto mb-3 text-gray-400" />
            <h3 class="text-base font-semibold text-gray-900 dark:text-white mb-1">
                Couldn't load your data
            </h3>
            <p class="text-sm text-gray-500 dark:text-gray-400 mb-4 break-words">
                {{ errorMessage || 'Something went wrong while running the queries.' }}
            </p>
            <button @click="$emit('run')" :disabled="isRunning"
                class="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50">
                Try again
            </button>
        </template>

        <!-- Safe default: run hasn't started (auto-run normally covers this) -->
        <template v-else>
            <Icon name="heroicons:chart-bar" class="w-10 h-10 mx-auto mb-3 text-gray-400" />
            <h3 class="text-base font-semibold text-gray-900 dark:text-white mb-1">
                See your data
            </h3>
            <p class="text-sm text-gray-500 dark:text-gray-400 mb-4">
                This dashboard shows data based on your access.
            </p>
            <button @click="$emit('run')" :disabled="isRunning"
                class="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 disabled:opacity-50">
                <Icon name="heroicons:arrow-path" class="w-4 h-4" />
                Run
            </button>
        </template>
    </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue';
import DataSourceIcon from '~/components/DataSourceIcon.vue';

const props = defineProps<{
    state: 'loading' | 'signin' | 'connect' | 'no_access' | 'error' | 'ready';
    reportId: string;
    isRunning?: boolean;
    // Data sources the run couldn't build the viewer's client for
    sourceErrors?: Array<{ data_source: string; data_source_id?: string; code?: string; error: string }>;
    errorMessage?: string | null;
    // Connection type of the report's identity-scoped source (e.g. "powerbi"),
    // for the sign-in state's brand icon
    sourceType?: string | null;
}>();

defineEmits<{ (e: 'run'): void }>();

const sourceNames = computed(() => {
    const names = (props.sourceErrors || []).map((s) => s.data_source);
    if (names.length === 0) return 'your data source';
    if (names.length === 1) return names[0];
    return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
});

const connectSourceId = computed(() => {
    const errs = props.sourceErrors || [];
    const missing = errs.find((s) => s.code === 'credentials_required') || errs[0];
    return missing?.data_source_id || null;
});
</script>
