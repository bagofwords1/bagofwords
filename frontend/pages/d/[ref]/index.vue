<template>
    <!-- ?host=<host application>: the dashboard inside a customer's application,
         signed in by that application (EmbeddedArtifactGate). -->
    <EmbeddedArtifactGate v-if="hostApplication" ref="gate" :host-application="hostApplication"
                          @context="hostContext = $event">
        <SharedDashboardView
            :key="`${$route.fullPath}#${reloads}`"
            source="dashboard"
            :id="String($route.params.ref)"
            :host-context="hostContext"
            @reload="reloads++"
            @unavailable="onUnavailable"
            @opened="onOpened"
        />
    </EmbeddedArtifactGate>
    <!-- A published dashboard: the /r page over the dashboard's endpoints.
         Remounted on `reload` (e.g. editor capabilities changed). -->
    <SharedDashboardView
        v-else
        :key="`${$route.fullPath}#${reloads}`"
        source="dashboard"
        :id="String($route.params.ref)"
        @reload="reloads++"
    />
</template>

<script setup lang="ts">
import SharedDashboardView from '~/components/dashboard/SharedDashboardView.vue';
import EmbeddedArtifactGate from '~/components/dashboard/EmbeddedArtifactGate.vue';
import type { EmbedLoadFailure } from '~/utils/embedSession';

definePageMeta({
    layout: false,
    auth: false
});

const route = useRoute();
const reloads = ref(0);
const hostApplication = computed(() => {
    const value = route.query.host;
    return typeof value === 'string' && /^host_[a-f0-9]{8,32}$/.test(value) ? value : null;
});
// What the host page says the user is looking at (a record id, a filter);
// handed to the artifact as ARTIFACT_DATA.host_context. Advisory only.
const hostContext = ref<Record<string, any> | null>(null);
// Told by the view when the artifact cannot be opened here (not published, gone, ...),
// and when it opened. The gate is the view's parent, so it is mounted by then;
// if not, say so -- the viewer would otherwise be left with a blank frame.
const gate = ref<InstanceType<typeof EmbeddedArtifactGate> | null>(null);
function onUnavailable(failure: EmbedLoadFailure) {
    if (!gate.value) { console.error('[embed] no gate to show why the artifact is unavailable:', failure); return; }
    gate.value.artifactUnavailable(failure);
}
function onOpened() {
    if (!gate.value) { console.error('[embed] no gate to tell the artifact opened'); return; }
    gate.value.artifactOpened();
}
</script>
