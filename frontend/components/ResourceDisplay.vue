<template>
  <div class="bg-gray-50 border border-gray-200 rounded-md p-3 text-sm">
    <DBTResourceDisplay v-if="isDbtResource" :resource="resource" />
    <LookMLResourceDisplay v-else-if="isLookMLResource" :resource="resource" />
    <MarkdownResourceDisplay v-else-if="isMarkdownResource" :resource="resource" />
    <GenericResourceDisplay v-else :resource="resource" />
  </div>
</template>

<script setup lang="ts">
import DBTResourceDisplay from '~/components/DBTResourceDisplay.vue';
import LookMLResourceDisplay from '~/components/LookMLResourceDisplay.vue';
import MarkdownResourceDisplay from '~/components/MarkdownResourceDisplay.vue';
import GenericResourceDisplay from '~/components/GenericResourceDisplay.vue';

interface Resource {
  id: string;
  name: string;
  resource_type: string;
  description?: string;
  path?: string;
  sql_content?: string;
  raw_data?: any;
  columns?: any[];
  depends_on?: string[];
}

interface Props {
  resource: Resource;
}

const props = defineProps<Props>();

const isDbtResource = computed(() => {
  const dbtTypes = ['model', 'model_config', 'dbt_model', 'dbt_metric', 'dbt_source', 'dbt_seed', 'dbt_macro', 'dbt_test', 'dbt_singular_test', 'dbt_exposure', 'metric', 'source', 'seed', 'macro', 'test', 'singular_test', 'exposure'];
  return dbtTypes.includes(props.resource.resource_type);
});

const isLookMLResource = computed(() => {
  return props.resource.resource_type.startsWith('lookml_');
});

const isMarkdownResource = computed(() => {
  return props.resource.resource_type === 'markdown' || props.resource.path?.endsWith('.md');
});
</script>