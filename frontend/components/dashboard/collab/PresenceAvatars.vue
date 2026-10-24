<template>
    <!-- Who else is editing this dashboard right now (collab01): an avatar
         per person, and a pulsing chip while someone's agent is at work. -->
    <div v-if="others.length" class="flex items-center gap-1.5" data-testid="dashboard-presence">
        <div
            v-for="agent in agents"
            :key="`agent-${agent.user.id}`"
            class="flex items-center gap-1 rounded-full border border-violet-200 dark:border-violet-800 bg-violet-50 dark:bg-violet-950/60 text-violet-700 dark:text-violet-300 ps-1.5 pe-2 py-0.5 text-[11px] whitespace-nowrap"
            data-testid="presence-agent"
        >
            <span class="relative flex w-2 h-2">
                <span class="absolute inline-flex h-full w-full rounded-full bg-violet-400 opacity-75 animate-ping"></span>
                <span class="relative inline-flex rounded-full w-2 h-2 bg-violet-500"></span>
            </span>
            <Icon name="heroicons:sparkles" class="w-3 h-3" />
            {{ $t('dashboardCollab.presence.agentEditing', { name: firstName(agent.user) }) }}
        </div>
        <UTooltip :text="label">
            <div class="flex -space-x-1.5 rtl:space-x-reverse">
                <span
                    v-for="person in shown"
                    :key="person.user.id"
                    :style="{ backgroundColor: colorFor(person.user.id) }"
                    class="inline-flex items-center justify-center w-6 h-6 rounded-full ring-2 ring-white dark:ring-gray-900 text-[10px] font-semibold text-white"
                    data-testid="presence-avatar"
                >{{ initials(person.user) }}</span>
                <span
                    v-if="others.length > shown.length"
                    class="inline-flex items-center justify-center w-6 h-6 rounded-full ring-2 ring-white dark:ring-gray-900 bg-gray-200 dark:bg-gray-700 text-[10px] text-gray-600 dark:text-gray-300"
                >+{{ others.length - shown.length }}</span>
            </div>
        </UTooltip>
    </div>
</template>

<script setup lang="ts">
import type { PresenceEntry, PresencePerson } from '~/composables/useDashboardPresence';

const props = defineProps<{ others: PresenceEntry[] }>();
const { t } = useI18n();

const shown = computed(() => props.others.slice(0, 4));
const agents = computed(() => props.others.filter((e) => e.state === 'agent_editing'));

const label = computed(() => {
    const first = props.others[0];
    if (!first) return '';
    if (props.others.length === 1) return t('dashboardCollab.presence.alsoEditing', { name: first.user.name || '' });
    return t('dashboardCollab.presence.alsoEditingMany', { name: first.user.name || '', count: props.others.length - 1 });
});

function firstName(person: PresencePerson) {
    return (person.name || '').split(/[\s@]/)[0] || person.name || '';
}

function initials(person: PresencePerson) {
    const parts = (person.name || '?').replace(/@.*/, '').split(/[\s._-]+/).filter(Boolean);
    return ((parts[0]?.[0] || '?') + (parts[1]?.[0] || '')).toUpperCase();
}

// A steady colour per person, so the same editor reads the same everywhere.
const PALETTE = ['#2563eb', '#16a34a', '#db2777', '#ea580c', '#7c3aed', '#0891b2', '#ca8a04', '#dc2626'];
function colorFor(id: string) {
    let h = 0;
    for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    return PALETTE[h % PALETTE.length];
}
</script>
