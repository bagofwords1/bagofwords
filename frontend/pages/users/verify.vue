<template>
    <div class="flex h-screen justify-center py-20 px-5 sm:px-0">
        <div class="w-full sm:w-1/3">
            <div>
                <Icon name="heroicons:envelope" class="w-10 h-10 text-green-500" />
            </div>
            <h1 class="font-bold text-lg">Verify your email</h1>
            <p class="mt-3 text-sm text-gray-700">
                A message with a confirmation link has been sent to your email address.<br /><br />
                Please follow the link to activate your account.
            </p>
        </div>
    </div>
</template>

<script setup lang="ts">
import { useOrganization } from '~/composables/useOrganization'

definePageMeta({
    layout: 'users'
})

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
        
        const { getSession } = useAuth()
        await getSession()

        const { organization } = useOrganization()
        
        if (organization.id) {
            navigateTo('/')
        } else {
            navigateTo('/organizations/new')
        }
    } catch (error) {
        console.error('Verification error:', error)
    }
}

onMounted(() => {
    const token = new URLSearchParams(window.location.search).get('token')
    if (token) {
        verify()
    }
})
</script>