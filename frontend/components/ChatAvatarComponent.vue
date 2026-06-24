<template>
    <div v-if="role == 'user'" class="h-7 w-7 flex items-center justify-center text-xs rounded-full inline-block overflow-hidden"
        :class="avatarUrl ? 'bg-gray-100 dark:bg-gray-800' : 'border border-blue-200 bg-blue-100 dark:bg-blue-900/50 text-blue-700 dark:text-blue-300 font-medium'">
        <img v-if="avatarUrl" :src="avatarUrl" alt="" class="h-7 w-7 rounded-full object-cover" />
        <span v-else>{{ initial }}</span>
    </div>
    <div v-else-if="role == 'system' || role == 'thinking'" class="h-7 w-7 flex font-bold items-center justify-center text-xs rounded-lg inline-block bg-contain bg-center bg-no-repeat" style="background-image: url('/assets/logo-128.png')">
    </div>
</template>

<script lang="ts" setup>
const props = defineProps<{
    role: string
    // Optional message author. When omitted (the common case — the completion
    // payload doesn't carry an author), we fall back to the current user.
    user?: Record<string, any>
}>()

const role = props.role

const { data: currentUser } = useAuth()
const author = computed<any>(() => props.user || currentUser.value)
const avatarUrl = computed<string | null>(() => author.value?.image_url || null)
const initial = computed<string>(() => {
    const name = author.value?.name || author.value?.email || 'U'
    return String(name).charAt(0).toUpperCase()
})
</script>
