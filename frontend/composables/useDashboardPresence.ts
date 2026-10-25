// Who is editing a dashboard right now (collab01; the API reference is
// docs/feedback-loops/2026-10-23-collaborative-dashboards-api.md).
//
// While `enabled` (an editor has the dashboard open), the browser beats
// PUT /dashboards/{id}/presence every 15 s -- with the conversation it is in,
// when it is in one -- and gets everyone's presence back in the same answer.
// Leaving (unmount, another dashboard, the tab closing) deletes its row; a row
// that stops beating counts as gone after 30 s on the server anyway.

export interface PresencePerson {
    id: string;
    name: string | null;
}

export interface PresenceEntry {
    user: PresencePerson;
    report_id: string | null;
    state: 'viewing' | 'agent_editing';
    element_keys: string[];
    seen_at: string | null;
    is_me: boolean;
}

const BEAT_MS = 15000;

export function useDashboardPresence(
    dashboardId: Ref<string | null | undefined>,
    reportId: Ref<string | null | undefined>,
    enabled: Ref<boolean>,
) {
    const entries = ref<PresenceEntry[]>([]);
    let timer: ReturnType<typeof setTimeout> | null = null;
    let alive = true;
    let beating: { id: string; report: string | null } | null = null;

    // Everyone else, one entry per person (a person in two conversations
    // counts once; an agent at work wins over a plain view).
    const others = computed<PresenceEntry[]>(() => {
        const byUser = new Map<string, PresenceEntry>();
        for (const e of entries.value) {
            if (e.is_me) continue;
            const seen = byUser.get(e.user.id);
            if (!seen || (e.state === 'agent_editing' && seen.state !== 'agent_editing')) byUser.set(e.user.id, e);
        }
        return [...byUser.values()];
    });
    // The charts someone else's agent is changing now.
    const agentKeys = computed<Set<string>>(() => new Set(
        others.value.filter((e) => e.state === 'agent_editing').flatMap((e) => e.element_keys || []),
    ));
    const agentEditors = computed(() => others.value.filter((e) => e.state === 'agent_editing'));

    function stop() {
        if (timer) clearTimeout(timer);
        timer = null;
    }

    async function leave() {
        const was = beating;
        beating = null;
        if (!was) return;
        const query = was.report ? `?report_id=${encodeURIComponent(was.report)}` : '';
        try {
            await useMyFetch(`/artifacts/${encodeURIComponent(was.id)}/presence${query}`, { method: 'DELETE' });
        } catch { /* best effort: the server ages it out */ }
    }

    async function beat() {
        stop();
        const id = dashboardId.value;
        if (!alive || !enabled.value || !id) return;
        const report = reportId.value || null;
        if (beating && (beating.id !== id || beating.report !== report)) await leave();
        try {
            const { data, error } = await useMyFetch(`/artifacts/${encodeURIComponent(id)}/presence`, {
                method: 'PUT',
                body: { report_id: report },
            });
            if (!alive) return;
            if (!error.value && data.value) {
                beating = { id, report };
                entries.value = ((data.value as any).entries || []) as PresenceEntry[];
            } else {
                entries.value = [];
            }
        } catch {
            entries.value = [];
        }
        if (alive && enabled.value) timer = setTimeout(beat, BEAT_MS);
    }

    watch([() => dashboardId.value, () => reportId.value, () => enabled.value], () => {
        if (enabled.value && dashboardId.value) beat();
        else { stop(); entries.value = []; leave(); }
    }, { immediate: true });

    const onUnload = () => { leave(); };
    if (process.client) window.addEventListener('beforeunload', onUnload);
    onBeforeUnmount(() => {
        alive = false;
        stop();
        if (process.client) window.removeEventListener('beforeunload', onUnload);
        leave();
    });

    return { entries, others, agentKeys, agentEditors, refresh: beat };
}
