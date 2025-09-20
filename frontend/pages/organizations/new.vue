<template>
    <div class="flex h-screen justify-center py-20 px-5 sm:px-0">
        <div class="w-full sm:w-1/4" v-if="pageReady">
            <h1 class="font-bold text-lg">Create Organization</h1>
            <form @submit.prevent="createOrg()">
                <div class="mb-4 mt-8">
                    <label for="name" class="block text-sm font-medium text-gray-700">Name</label>
                    <input v-model="name" type="text" id="name" name="name"
                        class="mt-1 block w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-indigo-500 focus:border-indigo-500 sm:text-sm"
                        required>
                </div>
                <div class="mt-3">
                    <button type="submit"
                        class="w-full flex justify-center py-2 px-4 border border-transparent rounded-md shadow-sm text-sm font-medium text-white bg-indigo-600 hover:bg-indigo-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-indigo-500">
                        Create organization
                    </button>
                </div>
            </form>
        </div>
        <div v-else>
            <h1>Loading...</h1>
        </div>
    </div>

</template>

<script setup lang="ts">

import { ref } from 'vue'
import { useAuth } from '#imports'

const pageReady = ref(false)
const { organization, ensureOrganization, fetchOrganization } = useOrganization()
const { getSession } = useAuth()

const name = ref('');

definePageMeta({
    auth: true,
    layout: 'users'
})

onMounted(async () => {
    const org = await fetchOrganization()
    if (org.id != null) {
        return navigateTo('/')
    }
    pageReady.value = true
})

async function createOrg() {
    const requestBody = {
        name: name.value,
    };

    try {
        const response = await useMyFetch('/organizations', { 
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify(requestBody),
        });

        if (response.error.value) {
            throw new Error(`Could not create organization: ${response.error.value}`);
        }
        
        // Refresh the session to get the new organization
        await getSession({ force: true })
        
        navigateTo('/onboarding')
    } catch (error) {
        console.error('Error during org creation:', error);
    }
}

</script>