<template>
    <div class="flex h-screen justify-center py-20 px-5 sm:px-0" v-if="pageLoaded">
        <div class="w-full sm:w-1/3">
            <template v-if="hasToken || smtpEnabled">
                <div>
                    <Icon name="heroicons:envelope" class="w-10 h-10 text-green-500" />
                </div>
                <h1 class="font-bold text-lg">Verify your email</h1>
                <p class="mt-3 text-sm text-gray-700">
                    A message with a confirmation link has been sent to your email address.<br /><br />
                    Please follow the link to activate your account.
                </p>
            </template>
            <template v-else>
                <div class="text-center">
                    <Icon name="heroicons:exclamation-triangle" class="w-10 h-10 text-yellow-500 mx-auto mb-3" />
                    <h1 class="font-bold text-lg">Email Verification Unavailable</h1>
                    <p class="mt-3 text-sm text-gray-700">
                        Email is not configured on this instance. Please contact your administrator for account verification.
                    </p>
                    <div class="mt-5">
                        <NuxtLink to="/users/sign-in" class="text-blue-400 hover:text-blue-600">
                            Back to Sign in
                        </NuxtLink>
                    </div>
                </div>
            </template>
        </div>
    </div>
    <div v-else class="flex h-screen items-center justify-center"><Spinner class="h-6 w-6" /></div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useOrganization } from '~/composables/useOrganization'
import Spinner from '~/components/Spinner.vue'

definePageMeta({
    layout: 'users'
})

const { getSession } = useAuth()
const { fetchOrganization } = useOrganization()

const smtpEnabled = ref(false)
const pageLoaded = ref(false)
const hasToken = ref(false)

async function verify() {
    try {
        const token = new URLSearchParams(window.location.search).get('token')
        
        if (!token) {
            throw new Error('No verification token provided')
        }
        
        const response = await $fetch('/api/auth/verify', {
            method: 'POST',
            body: { token }
        })
        
        await getSession({ force: true })
        
        const org = await fetchOrganization()
        if (org?.id) {
            navigateTo('/')
        } else {
            navigateTo('/organizations/new')
        }
    } catch (error) {
        console.error('Verification error:', error)
    }
}

onMounted(async () => {
    const token = new URLSearchParams(window.location.search).get('token')
    hasToken.value = !!token
    
    if (token) {
        verify()
    } else {
        try {
            const settings = await $fetch('/api/settings')
            smtpEnabled.value = settings?.smtp_enabled ?? false
        } catch (_) {}
    }
    pageLoaded.value = true
})
</script>
