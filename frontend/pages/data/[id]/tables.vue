<template>
    <div class="py-6">
        <!-- Hide content when there's a fetch error (layout shows error state) -->
        <div v-if="injectedFetchError" />
        <div v-else class="border border-gray-200 rounded-lg p-6">
            <TablesSelector :ds-id="id" :schema="schemaMode" :can-update="canUpdateDataSource" :show-refresh="true" :show-save="canUpdateDataSource" :show-header="true" header-title="Select tables" header-subtitle="Choose which tables to enable" save-label="Save" :show-stats="true" @saved="onSaved" />
        </div>
    </div>
    
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'data' })
import TablesSelector from '@/components/datasources/TablesSelector.vue'
import { useCan, usePermissionsLoaded } from '~/composables/usePermissions'
import type { Ref } from 'vue'

const toast = useToast()
const route = useRoute()
const id = computed(() => String(route.params.id || ''))

// Inject integration data from layout (avoid duplicate API calls)
const injectedIntegration = inject<Ref<any>>('integration', ref(null))
const injectedFetchError = inject<Ref<number | null>>('fetchError', ref(null))

const loading = ref(false)
const schemaMode = ref<'full' | 'user'>('full')

const permissionsLoaded = usePermissionsLoaded()
const canUpdateDataSource = computed(() => useCan('update_data_source'))

// Tables state is managed by TablesSelector component

// Set schema mode based on permissions - wait for permissions to load
watch([injectedIntegration, permissionsLoaded], ([ds, loaded]) => {
    if (ds && loaded) {
        schemaMode.value = canUpdateDataSource.value ? 'full' : 'user'
    }
}, { immediate: true })

function onSaved() { toast.add({ title: 'Saved', description: 'Schema updated', color: 'green' }) }
</script>


