<template>
    <div class="py-6">
        <div class="border border-gray-200 rounded-lg p-6">
            <TablesSelector :ds-id="id" :schema="schemaMode" :can-update="canUpdateDataSource" :show-refresh="true" :show-save="canUpdateDataSource" :show-header="true" header-title="Select tables" header-subtitle="Choose which tables to enable" save-label="Save" :show-stats="true" @saved="onSaved" />
        </div>
    </div>
    
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'integrations' })
import TablesSelector from '@/components/datasources/TablesSelector.vue'
import { useCan } from '~/composables/usePermissions'
const toast = useToast()

const route = useRoute()
const id = computed(() => String(route.params.id || ''))

const loading = ref(false)
const schemaMode = ref<'full' | 'user'>('full')

const canUpdateDataSource = computed(() => useCan('update_data_source'))

// Tables state is managed by TablesSelector component

onMounted(async () => {
    if (!id.value) return
    loading.value = true
    try {
        const policy = await getAuthPolicy()
        if(canUpdateDataSource.value) {
            schemaMode.value = 'full'
        } else {
            schemaMode.value = 'user'
        }
    } finally {
        loading.value = false
    }
})

async function getAuthPolicy() {
    try {
        const res = await useMyFetch(`/data_sources/${id.value}`, { method: 'GET' })
        if (!(res as any)?.error?.value) {
            const ds = (res as any).data?.value as any
            return (ds?.auth_policy as any) ?? null
        }
    } catch {}
    return null
}

function onSaved() { toast.add({ title: 'Saved', description: 'Schema updated', color: 'green' }) }
</script>


