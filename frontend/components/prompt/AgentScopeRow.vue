<template>
  <button type="button" class="w-full text-start px-2 py-1.5 rounded hover:bg-gray-50 dark:hover:bg-gray-800 cursor-pointer flex items-center justify-between gap-2"
      @click="$emit('select')"
      @mouseenter="$emit('hover', $event)"
      @mouseleave="$emit('leave')"
      data-testid="agent-scope-option"
      :data-agent-name="ds.name"
      :aria-pressed="selected"
      :data-selected="selected ? 'true' : 'false'"
  >
      <div class="flex items-center min-w-0">
          <DataSourceIcon :type="ds.type" :connector-key="ds.connector_key" :icon-token="ds.icon_token" :icon="ds.icon" class="h-4 flex-shrink-0" />
          <span class="ms-2 truncate">{{ ds.name }}<span v-if="parentPath" class="block truncate text-2xs text-ink-muted">{{ parentPath }}</span></span>
      </div>
      <!-- Badges sit at the far end of the row (before the check) so
           they line up in a column instead of trailing each name. -->
      <div class="flex items-center gap-1.5 flex-shrink-0">
          <!-- Non-production agents only reach here for managers; flag
               the lifecycle stage so it's clear they aren't live for
               consumers yet. Mirrors the agent page (Development /
               Training / Disabled), not the raw publish_status. -->
          <span
              v-if="stage !== 'production'"
              :class="['text-2xs rounded border px-1 py-0.5', stageMeta(stage).badge]"
          >{{ $t('agentsPage.stage.' + stage) }}</span>
          <!-- Running via the connection's system (service principal) creds -->
          <span
              v-if="serviceAccount"
              class="text-2xs text-ink-muted border border-gray-200 dark:border-gray-700 rounded px-1 py-0.5"
          >{{ $t('data.serviceAccount') }}</span>
          <Icon v-if="selected" name="heroicons-check" class="w-4 h-4 text-blue-500" />
      </div>
  </button>
</template>
<script setup lang="ts">
import { stageMeta, type AgentStage } from '~/composables/useDataSourcePublishStatus'
defineProps<{ds: any, stage: AgentStage, selected: boolean, serviceAccount: boolean, parentPath?: string}>()
defineEmits<{select: [], hover: [event: MouseEvent], leave: []}>()
</script>
