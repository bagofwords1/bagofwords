<template>

  <div class="min-h-screen py-10 px-4 md:w-1/2 mx-auto text-sm">
      <div class="w-full px-4 pl-0 py-4">
      <div>
        <h1 class="text-lg font-semibold text-center">Integrations</h1>
        <p class="mt-4 text-gray-500 text-center">Connect and manage your data sources</p>
      </div>
        <WizardSteps class="mb-5 mt-4" current="schema" :ds-id="id" />


      <div class="bg-white rounded-lg">
        <TablesSelector :ds-id="id" schema="full" :can-update="true" :show-refresh="true" :show-save="true" :show-header="true" header-title="Select tables" header-subtitle="Choose which tables to enable" save-label="Save & Continue" @saved="onSaved" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true })
import WizardSteps from '@/components/datasources/WizardSteps.vue'
import TablesSelector from '@/components/datasources/TablesSelector.vue'
const route = useRoute()
const router = useRouter()
const id = computed(() => String(route.params.id || ''))

function onSaved() { router.replace(`/integrations/new/${id.value}/context`) }
</script>


