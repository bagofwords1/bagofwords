<template>
    <div class="flex pl-2 md:pl-4 text-sm">
        <div class="w-full md:w-3/4 px-4 pl-0 py-4">
            <div>
                <h1 class="text-lg font-semibold">
                    <GoBackChevron v-if="isExcel" />
          Memory
        </h1>
        <p class="mt-2 text-gray-500">Manage your organization memory</p>

      </div>
      <div class="bg-white rounded-lg shadow mt-8">

        <table class="min-w-full divide-y divide-gray-200">
          <thead class="bg-gray-50">
            <tr>
              <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Memory</th>
              <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Description</th>
              <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">User</th>
              <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Created At</th>
              <th class="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Actions</th>
            </tr>
          </thead>
          <tbody class="bg-white divide-y divide-gray-200">
            <tr v-for="memory in memories" :key="memory.id" class="hover:bg-gray-50">
              <td class="px-6 py-4 whitespace-nowrap">
                <NuxtLink :to="{ path: `/memory/${memory.id}` }" class="text-sm font-medium text-gray-900 hover:text-blue-500">
                  {{ memory.title }}
                </NuxtLink>
              </td>
              <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">{{ memory.description }}</td>
              <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">{{ memory.user.name }}</td>
              <td class="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                {{ new Date(memory.created_at).toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' }) }}
              </td>
              <td class="px-6 py-4 whitespace-nowrap text-sm">
                <a href="" class="text-blue-500 hover:text-blue-700 mr-2">Use</a>
                <a href="" class="text-blue-500 hover:text-blue-700">Refresh</a>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import GoBackChevron from '@/components/excel/GoBackChevron.vue';

const { isExcel } = useExcel()


interface Memory {
  id: number
  title: string
  step_id: string
  user_id: string
  organization_id: string
  created_at: string
}

const memories = ref<Memory[]>([])

definePageMeta({ auth: true })

const getMemories = async () => {
  const response = await useMyFetch('/api/memories', {
    method: 'GET',
  })
  memories.value = response.data.value
}


onMounted(async () => {
  nextTick(async () => {
    await getMemories()
  })
})
</script>
