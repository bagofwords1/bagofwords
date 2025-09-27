<template>
  <div class="flex pl-2 md:pl-4 text-sm mx-auto md:w-1/2 md:pt-10">
    <div class="w-full px-4 pl-0 py-4">
      <div>
        <h1 class="text-lg font-semibold text-center">Integrations</h1>
        <p class="mt-4 text-gray-500 text-center">Connect and manage your data sources</p>
      </div>

      <WizardSteps class="mt-4" current="connect" />

      <div class="mt-6">
        <ConnectForm @success="handleSuccess" :forceShowSystemCredentials="true" :showRequireUserAuthToggle="true" :initialRequireUserAuth="false" :showTestButton="true" :showLLMToggle="true" :allowNameEdit="true" mode="create" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true })
import ConnectForm from '@/components/datasources/ConnectForm.vue'
import WizardSteps from '@/components/datasources/WizardSteps.vue'

function handleSuccess(ds: any) {
  const id = ds?.id
  if (id) {
    navigateTo(`/integrations/new/${id}/schema`)
  } else {
    navigateTo('/integrations')
  }
}
</script>


