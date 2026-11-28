<template>
    <!-- The config fields a compute type fills in itself (schema `readOnly`),
         e.g. the generated SSH public key: shown to copy, never inputs. -->
    <div v-if="rows.length" class="space-y-3">
        <div v-for="row in rows" :key="row.key" :data-testid="`sandbox-readonly-${row.key}`">
            <div class="text-sm font-medium text-ink-2 mb-1.5">{{ row.title }}</div>
            <SandboxCopyBox :value="row.value" :testid="`sandbox-readonly-value-${row.key}`" />
            <p v-if="row.description" class="mt-1.5 text-xs text-ink-muted leading-relaxed">{{ row.description }}</p>
        </div>
    </div>
</template>

<script setup lang="ts">
import type { SandboxFormSchema } from '~/composables/useSandboxApi'
import SandboxCopyBox from '~/components/sandbox/SandboxCopyBox.vue'

const props = defineProps<{ schema: SandboxFormSchema; config: Record<string, any> }>()

const rows = computed(() =>
    Object.entries(props.schema?.properties || {})
        .filter(([key, prop]) => prop.readOnly && props.config?.[key])
        .map(([key, prop]) => ({
            key,
            title: prop.title || key,
            description: prop.description || '',
            value: String(props.config[key]),
        }))
)
</script>
