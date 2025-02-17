<template>
    <div class="mt-4" v-if="subscription">
        <h2 class="text-lg font-semibold mb-4">License Information</h2>
        
        <div class="flex">
            <div class="flex-1 leading-relaxed bg-white rounded-lg shadow p-6">
                <h3 class="text-sm font-medium text-gray-900 mb-3">
                    {{ subscription.plan_config.name }}
                    <span class="bg-green-200 text-green-600 font-normal text-xs p-1 rounded-md">Active</span>
                </h3>
                
                <div class="space-y-2 text-sm text-gray-600">
                    <div>Licensed seats: {{ subscription.plan_config.limits.users.toLocaleString() }}</div>
                    <div>Available tasks: {{ subscription.plan_config.limits.tasks.toLocaleString() }}</div>
                    <div>License Key: 
                        <span class="font-mono text-xs bg-gray-100 px-2 py-0.5 rounded">
                            {{ subscription.license_key || 'Free plan' }}
                        </span>
                    </div>
                </div>
                <div class="mt-4">
                <a 
                    href="mailto:hi@bagofwords.com"
                    class="mt-6 text-xs text-gray-500 hover:text-gray-700 border border-gray-200 px-3 py-1.5 rounded-md inline-flex items-center gap-2"
                >
                    <Icon name="heroicons:envelope" class="w-4 h-4" />
                    Contact our team for an Enterprise license
                </a>
            </div>
                <button 
                    @click="enterLicenseKey = true"
                    class="mt-6 hidden border border-blue-500 px-3 py-1.5 text-xs font-medium rounded-md transition-colors bg-white text-blue-500 hover:bg-blue-50 active:bg-blue-100"
                >
                    Update License Key
                </button>

                <UModal v-model="enterLicenseKey">
                    <div class="p-4 relative">
                        <button @click="enterLicenseKey = false"
                            class="absolute top-2 right-2 text-gray-500 hover:text-gray-700 outline-none">
                            <Icon name="heroicons:x-mark" class="w-5 h-5" />
                        </button>
                        <h1 class="text-lg font-semibold">Update License Key</h1>
                        <p class="text-sm text-gray-500">Enter your new license key to update your subscription</p>
                        
                        <div class="mt-6">
                            <label class="block text-sm font-medium text-gray-700 mb-2">License Key</label>
                            <input 
                                v-model="licenseKey" 
                                class="w-full rounded-md border border-gray-200 px-3 py-2 text-sm"
                                placeholder="Enter your license key"
                            />
                        </div>

                        <div class="border-t border-gray-200 pt-4 mt-8">
                            <div class="flex justify-end space-x-2">
                                <button 
                                    @click="enterLicenseKey = false"
                                    class="px-3 py-1.5 text-xs font-medium text-gray-700 bg-white border border-gray-300 rounded-md hover:bg-gray-50"
                                >
                                    Cancel
                                </button>
                                <button 
                                    @click="updateLicenseKey"
                                    class="px-3 py-1.5 text-xs font-medium text-white bg-blue-500 border border-transparent rounded-md hover:bg-blue-600"
                                >
                                    Update License
                                </button>
                            </div>
                        </div>
                    </div>
                </UModal>
            </div>
        </div>
    </div>
    <div v-else class="mt-4 p-6 bg-gray-50 rounded-lg">
        <p class="text-gray-500 text-sm">No active license found</p>
    </div>
</template>

<script setup lang="ts">
const subscription = ref(null)
const currentSeatCount = ref(0)
const enterLicenseKey = ref(false)
const licenseKey = ref('')

async function getSubscription() {
    const response = await useMyFetch('/api/subscription')
    subscription.value = response.data.value
    currentSeatCount.value = subscription.value.plan_config.limits.users
    licenseKey.value = subscription.value.license_key
}

async function updateLicenseKey() {
    const response = await useMyFetch('/api/subscription', {
        method: 'POST',
        body: JSON.stringify({ license_key: licenseKey.value }),
    })
}

onMounted(async () => {
    nextTick(async () => {
        await getSubscription()
    })
})

</script>