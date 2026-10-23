<template>
    <!-- A dashboard's history (collab01): the conversations that edited it,
         its versions (preview any), the releases kept for Roll back, its
         notes and its owner. Contract used by the dashboard page and the
         in-conversation frame:
           v-model (open), dashboard-id, capabilities (from the page meta),
           @rolled-back (readers now see another release), @transferred. -->
    <UModal v-model="open" :ui="{ width: 'sm:max-w-3xl' }">
        <div class="p-4 sm:p-5 flex flex-col min-h-0" data-testid="dashboard-history">
            <!-- Header -->
            <div class="flex items-start justify-between gap-3 mb-3">
                <div class="min-w-0">
                    <h2 class="text-base font-semibold text-gray-900 dark:text-white">{{ $t('dashboardCollab.history.title') }}</h2>
                    <p class="text-sm text-gray-400">{{ $t('dashboardCollab.history.subtitle') }}</p>
                </div>
                <button type="button" :aria-label="$t('common.close')" data-testid="dashboard-history-close"
                    class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 outline-none shrink-0" @click="open = false">
                    <Icon name="heroicons:x-mark" class="w-5 h-5" />
                </button>
            </div>

            <!-- Tabs: scroll sideways on a phone rather than wrap. -->
            <div class="-mx-4 sm:mx-0 px-4 sm:px-0 overflow-x-auto border-b border-gray-200 dark:border-gray-700 mb-3">
                <div class="flex gap-1 min-w-max" role="tablist">
                    <button v-for="tab in tabs" :key="tab.key" type="button" role="tab"
                        :aria-selected="active === tab.key" :data-testid="`history-tab-${tab.key}`"
                        class="flex items-center gap-1.5 px-2.5 py-2 text-xs font-medium border-b-2 -mb-px whitespace-nowrap outline-none focus-visible:ring-2 focus-visible:ring-primary-500 rounded-t"
                        :class="active === tab.key
                            ? 'border-primary-500 text-primary-600 dark:text-primary-400'
                            : 'border-transparent text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200'"
                        @click="active = tab.key">
                        <Icon :name="tab.icon" class="w-3.5 h-3.5" />
                        <span>{{ tab.label }}</span>
                        <span v-if="tab.count != null" class="text-[10px] tabular-nums text-gray-400">{{ tab.count }}</span>
                    </button>
                </div>
            </div>

            <div class="max-h-[65vh] overflow-y-auto -mx-1 px-1">
                <!-- Notes and Owner read their own data. -->
                <HistoryNotes v-if="active === 'notes'" :dashboard-id="dashboardId" :can-edit="can('settings')" />
                <HistoryOwner v-else-if="active === 'owner'" :dashboard-id="dashboardId" @transferred="onTransferred" />

                <div v-else-if="loading && !history" class="flex items-center gap-2 py-10 justify-center text-sm text-gray-400">
                    <Spinner class="w-4 h-4" /> {{ $t('common.loading') }}
                </div>
                <div v-else-if="loadError && !history" class="py-10 flex flex-col items-center gap-2 text-sm text-gray-500 dark:text-gray-400"
                    data-testid="history-load-error">
                    <span>{{ loadError }}</span>
                    <UButton size="xs" color="gray" variant="soft" @click="load">{{ $t('common.retry') }}</UButton>
                </div>
                <template v-else-if="history">
                    <HistoryVersions v-if="active === 'versions'" :versions="history.versions" @preview="preview" />
                    <HistoryReleases v-else-if="active === 'published'" :dashboard-id="dashboardId" :releases="history.releases"
                        :can-rollback="can('publish')" @rolled-back="onRolledBack" />
                    <HistoryConversations v-else-if="active === 'conversations'" :conversations="history.conversations" />
                </template>
            </div>
            <slot />
        </div>
    </UModal>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue';
import HistoryVersions from './history/HistoryVersions.vue';
import HistoryReleases from './history/HistoryReleases.vue';
import HistoryConversations from './history/HistoryConversations.vue';
import HistoryNotes from './history/HistoryNotes.vue';
import HistoryOwner from './history/HistoryOwner.vue';

const props = defineProps<{ modelValue: boolean; dashboardId: string; capabilities: string[] }>();
const emit = defineEmits<{
    (e: 'update:modelValue', value: boolean): void;
    (e: 'rolled-back'): void;
    (e: 'transferred'): void;
}>();

const open = computed({
    get: () => props.modelValue,
    set: (value: boolean) => emit('update:modelValue', value),
});

const { t } = useI18n();
const { getErrorMessage } = useErrorMessage();

const can = (cap: string) => (props.capabilities || []).includes(cap);

type TabKey = 'versions' | 'published' | 'conversations' | 'notes' | 'owner';
const active = ref<TabKey>('versions');
const history = ref<any | null>(null);
const loading = ref(false);
const loadError = ref<string | null>(null);

const tabs = computed(() => {
    const h = history.value;
    const list: { key: TabKey; label: string; icon: string; count?: number | null }[] = [
        { key: 'versions', label: t('dashboardCollab.history.tabVersions'), icon: 'heroicons:clock', count: h ? h.versions?.length ?? 0 : null },
        { key: 'published', label: t('dashboardCollab.history.tabPublished'), icon: 'heroicons:rocket-launch', count: h ? h.releases?.length ?? 0 : null },
        { key: 'conversations', label: t('dashboardCollab.history.tabConversations'), icon: 'heroicons:chat-bubble-left-right', count: h ? h.conversations?.length ?? 0 : null },
        { key: 'notes', label: t('dashboardCollab.history.tabNotes'), icon: 'heroicons:document-text' },
    ];
    if (can('transfer')) list.push({ key: 'owner', label: t('dashboardCollab.history.tabOwner'), icon: 'heroicons:user-circle' });
    return list;
});
// Owner is gone once the reader may no longer transfer (e.g. just did).
watch(tabs, (list) => { if (!list.some(tab => tab.key === active.value)) active.value = 'versions'; });

let seq = 0;
async function load() {
    if (!props.dashboardId) return;
    const mine = ++seq;
    loading.value = true;
    loadError.value = null;
    try {
        const res = await useMyFetch(`/dashboards/${props.dashboardId}/history`);
        if (mine !== seq) return;
        if (res.error.value) throw res.error.value;
        history.value = res.data.value || { conversations: [], releases: [], versions: [], drafts: [] };
    } catch (e) {
        if (mine === seq) loadError.value = getErrorMessage(e, t('dashboardCollab.history.loadFailed'));
    } finally {
        if (mine === seq) loading.value = false;
    }
}

function preview(number: number) {
    open.value = false;
    navigateTo(`/d/${props.dashboardId}/draft?version=${number}`);
}

async function onRolledBack() {
    emit('rolled-back');
    await load();
}

async function onTransferred() {
    emit('transferred');
    await load();
}

// Read on every open: the history moves while people edit.
watch(() => props.modelValue, (isOpen) => { if (isOpen) load(); }, { immediate: true });
watch(() => props.dashboardId, () => {
    history.value = null;
    active.value = 'versions';
    if (props.modelValue) load();
});
</script>
