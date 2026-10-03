<template>
  <main class="h-screen w-screen overflow-hidden">
    <ArtifactFrame v-if="artifact" :report-id="artifact.report_id"
      :requested-artifact-id="artifact.id" :artifacts="[artifact]"
      :latest-artifact="artifact" verification-preview />
    <div v-else-if="error" role="alert" class="p-6 text-sm text-red-600">{{ $t('tools.browser.previewUnavailable') }}</div>
  </main>
</template>

<script setup lang="ts">
import ArtifactFrame from '~/components/dashboard/ArtifactFrame.vue'
// This page contains no application session. Only the server-side preview
// broker can authorize its artifact/API requests; a normal anonymous visit
// receives the ordinary API authentication failure.
definePageMeta({ layout: false, auth: false })
const route = useRoute()
const { data: artifact, error } = await useFetch<any>(`/api/artifacts/${encodeURIComponent(String(route.params.id))}`, { server: false })
</script>
