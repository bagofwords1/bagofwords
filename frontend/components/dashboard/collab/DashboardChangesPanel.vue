<template>
    <!-- Review changes (collab01): every chart of the version on screen, by
         how it compares with the live release, with who changed it last.
         Hovering an entry outlines its chart on the page when the page lets
         us find it. -->
    <aside
        class="absolute top-0 bottom-0 end-0 z-30 w-full sm:w-80 bg-white dark:bg-gray-900 border-s border-gray-200 dark:border-gray-700 shadow-xl flex flex-col"
        data-testid="changes-panel"
        :aria-label="$t('dashboardCollab.page.changesTitle')"
    >
        <header class="flex items-center justify-between gap-2 px-4 py-2.5 border-b border-gray-100 dark:border-gray-800">
            <h2 class="text-sm font-semibold text-gray-900 dark:text-gray-100">{{ $t('dashboardCollab.page.changesTitle') }}</h2>
            <button type="button" class="p-1 text-gray-400 hover:text-gray-600 rounded" :aria-label="$t('common.close')" @click="$emit('close')">
                <Icon name="heroicons:x-mark" class="w-4 h-4" />
            </button>
        </header>

        <div class="flex-1 min-h-0 overflow-y-auto px-4 py-3 space-y-4 text-xs">
            <div v-if="changes?.published_since"
                class="flex items-start gap-2 rounded-lg border border-amber-200 dark:border-amber-700 bg-amber-50 dark:bg-amber-900/40 text-amber-800 dark:text-amber-200 px-3 py-2"
                data-testid="changes-published-since">
                <Icon name="heroicons:exclamation-triangle" class="w-4 h-4 shrink-0 mt-0.5" />
                <span>{{ $t('dashboardCollab.page.publishedSince', {
                    name: changes.published_since.by?.name || $t('dashboardCollab.page.someone'),
                    when: changes.published_since.at ? relativeTime(changes.published_since.at) : '',
                }) }}</span>
            </div>

            <div v-if="loading && !changes" class="flex items-center gap-2 text-gray-400">
                <Icon name="heroicons:arrow-path" class="w-3.5 h-3.5 animate-spin" />
                {{ $t('common.loading') }}
            </div>
            <p v-else-if="error" role="alert" class="text-red-600">{{ error }}</p>
            <p v-else-if="changes && !groups.length" class="text-gray-400">{{ $t('dashboardCollab.page.noCharts') }}</p>

            <section v-for="group in groups" :key="group.kind" :data-testid="`changes-group-${group.kind}`">
                <h3 class="flex items-center gap-1.5 mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
                    <span :class="['w-2 h-2 rounded-full', dot[group.kind]]"></span>
                    {{ $t(`dashboardCollab.page.group.${group.kind}`) }}
                    <span class="font-normal text-gray-400">({{ group.items.length }})</span>
                </h3>
                <ul class="space-y-1">
                    <li v-for="item in group.items" :key="`${group.kind}-${item.key}`">
                        <button
                            type="button"
                            class="w-full text-start rounded-md px-2 py-1.5 hover:bg-gray-50 dark:hover:bg-gray-800 focus:bg-gray-50 dark:focus:bg-gray-800 outline-none"
                            :class="activeKey === item.key ? 'bg-amber-50 dark:bg-amber-900/30' : ''"
                            :data-chart-key="item.key"
                            :data-located="located.has(item.key) ? 'true' : 'false'"
                            @mouseenter="$emit('highlight', item.key)"
                            @mouseleave="$emit('highlight', null)"
                            @focus="$emit('highlight', item.key)"
                            @blur="$emit('highlight', null)"
                        >
                            <div class="flex items-center gap-1.5">
                                <span dir="auto" :class="['font-medium truncate', group.kind === 'removed' ? 'line-through text-gray-400' : 'text-gray-800 dark:text-gray-100']">
                                    {{ item.title || $t('dashboardCollab.page.untitled') }}
                                </span>
                                <Icon v-if="located.has(item.key)" name="heroicons:viewfinder-circle" class="w-3 h-3 shrink-0 text-gray-300" />
                            </div>
                            <div v-if="item.changed_by || item.version_number" class="text-[11px] text-gray-500 dark:text-gray-400 truncate">
                                {{ changedLine(item) }}
                            </div>
                        </button>
                    </li>
                </ul>
            </section>
        </div>
    </aside>
</template>

<script setup lang="ts">
type Kind = 'added' | 'changed' | 'removed' | 'unchanged';

const props = defineProps<{
    changes: any | null;
    loading: boolean;
    error: string | null;
    located: Set<string>;
    activeKey: string | null;
}>();
defineEmits<{ (e: 'close'): void; (e: 'highlight', key: string | null): void }>();

const { t } = useI18n();
const { relativeTime } = useRelativeTime();

const dot: Record<Kind, string> = {
    added: 'bg-emerald-500',
    changed: 'bg-amber-500',
    removed: 'bg-red-500',
    unchanged: 'bg-gray-300 dark:bg-gray-600',
};

const groups = computed(() => {
    const c = props.changes;
    if (!c) return [];
    const order: Kind[] = ['added', 'changed', 'removed', 'unchanged'];
    const all = [...(c.elements || []), ...(c.removed_since_live || [])];
    return order
        .map((kind) => ({ kind, items: all.filter((e: any) => (e.vs_live || 'unchanged') === kind) }))
        .filter((g) => g.items.length);
});

function changedLine(item: any) {
    return t('dashboardCollab.page.changedBy', {
        name: item.changed_by?.name || t('dashboardCollab.page.someone'),
        when: item.changed_at ? relativeTime(item.changed_at) : '',
        n: item.version_number ?? '',
    });
}
</script>
