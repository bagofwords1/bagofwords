<template>
    <div class="mt-6">
        <h2 class="text-lg font-medium text-gray-900">General
            <p class="text-sm text-gray-500 font-normal mb-8">
                Manage workspace identity and preferences.
            </p>
        </h2>

        <div v-if="loading" class="py-4">
            <ULoader />
        </div>

        <UAlert v-if="error" class="mt-4" type="danger">
            {{ error }}
        </UAlert>

        <div v-if="!loading && !error" class="space-y-6">
            <!-- Organization Name -->
            <div class="md:w-2/3 space-y-2">
                <div class="text-sm font-medium text-gray-800">Organization name</div>
                <UInput v-model="form.organization_name" :maxlength="80" placeholder="Workspace name" />
            </div>
            <!-- Organization Icon -->
            <div class="md:w-2/3 space-y-2">
                <div class="text-sm font-medium text-gray-800">Organization icon</div>
                <div class="flex items-center space-x-4">
                    <div class="w-14 h-14 rounded border bg-white overflow-hidden flex items-center justify-center">
                        <img v-if="form.icon_url" :src="form.icon_url" class="w-full h-full object-cover" />
                        <Icon v-else name="heroicons:building-office" class="w-6 h-6 text-gray-400" />
                    </div>
                    <div class="space-x-2">
                        <UButton size="sm" variant="outline" color="blue" @click="selectIcon">{{ form.icon_url ? 'Change' : 'Upload' }} Icon</UButton>
                        <UButton v-if="form.icon_url" size="sm" color="red" variant="soft" @click="queueRemoveIcon">Remove</UButton>
                        <input ref="fileInput" type="file" accept="image/*" class="hidden" @change="onIconSelected" />
                    </div>
                </div>
                <div class="text-xs text-gray-500">Square PNG/JPEG, ≤ 512 KB.</div>
            </div>



            <!-- AI Analyst Name -->
            <div class="md:w-2/3 space-y-2">
                <div class="text-sm font-medium text-gray-800">AI analyst name</div>
                <UInput v-model="form.ai_analyst_name" :maxlength="50" placeholder="AI Analyst" />
            </div>

            <!-- Credit toggle -->
            <div class="md:w-2/3 flex items-center justify-between">
                <div class="text-sm text-gray-800">Show "Made with Bag of words" credit</div>
                <UToggle v-model="form.bow_credit" />
            </div>

            <div class="md:w-2/3 pt-2">
                <UButton color="blue" @click="saveAll" :loading="saving">Save changes</UButton>
            </div>
        </div>
    </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useToast } from '#imports'

interface GeneralConfig {
    ai_analyst_name: string
    bow_credit: boolean
    icon_url?: string | null
    icon_key?: string | null
}

interface SettingsResponse {
    config?: { general?: GeneralConfig }
}

definePageMeta({ auth: true, permissions: ['manage_organization_settings','view_organization_settings'], layout: 'settings' })

const loading = ref(true)
const error = ref('')
const general = ref<GeneralConfig>({ ai_analyst_name: 'AI Analyst', bow_credit: true })
const form = ref<{ organization_name?: string } & GeneralConfig>({ ai_analyst_name: 'AI Analyst', bow_credit: true })
const pendingIconFile = ref<File | null>(null)
const removeIcon = ref(false)
const saving = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const toast = useToast()

const fetchSettings = async () => {
    loading.value = true
    error.value = ''
    try {
        const response = await useMyFetch('/api/organization/settings')
        if (response.status.value !== 'success') throw new Error(response.error?.value?.data?.message || 'Failed to fetch settings')
        const cfg = (response.data.value as SettingsResponse)?.config
        general.value = cfg?.general || { ai_analyst_name: 'AI Analyst', bow_credit: true }
        // Fetch current organization name from session if available
        const { organization } = useOrganization()
        form.value = { organization_name: organization.value?.name, ...general.value }
    } catch (e: any) {
        error.value = e.message || 'Failed to load settings'
        toast.add({ title: 'Error', description: error.value, color: 'red' })
    } finally {
        loading.value = false
    }
}

const saveAll = async () => {
    saving.value = true
    try {
        // 1) If a new icon is selected or removal queued, handle icon first
        if (pendingIconFile.value) {
            const formData = new FormData()
            formData.append('icon', pendingIconFile.value)
            const upload = await useMyFetch('/api/organization/general/icon', { method: 'POST', body: formData })
            if (upload.status.value !== 'success') throw new Error(upload.error?.value?.data?.message || 'Upload failed')
            const cfg = (upload.data.value as SettingsResponse)?.config
            form.value.icon_url = cfg?.general?.icon_url || form.value.icon_url
            form.value.icon_key = cfg?.general?.icon_key || form.value.icon_key
        } else if (removeIcon.value) {
            const remove = await useMyFetch('/api/organization/general/icon', { method: 'DELETE' })
            if (remove.status.value !== 'success') throw new Error(remove.error?.value?.data?.message || 'Remove failed')
            form.value.icon_url = null
            form.value.icon_key = null
        }

        // 2) Save organization name (if changed)
        if (form.value.organization_name) {
            await useMyFetch('/api/organization', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: form.value.organization_name }) })
        }

        // 3) Save textual and toggle settings
        const payload = { config: { general: { ai_analyst_name: form.value.ai_analyst_name, bow_credit: form.value.bow_credit, icon_key: form.value.icon_key, icon_url: form.value.icon_url } } }
        const response = await useMyFetch('/api/organization/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
        if (response.status.value !== 'success') throw new Error(response.error?.value?.data?.message || 'Failed to update settings')

        general.value = ((response.data.value as SettingsResponse)?.config?.general) || form.value
        toast.add({ title: 'Saved', color: 'green' })
        // reload to reflect icon in default layout
        window.location.reload()
    } catch (e: any) {
        toast.add({ title: 'Error', description: e.message || 'Failed to save', color: 'red' })
    } finally {
        saving.value = false
        pendingIconFile.value = null
        removeIcon.value = false
    }
}

const selectIcon = () => fileInput.value?.click()

const onIconSelected = async (evt: Event) => {
    const input = evt.target as HTMLInputElement
    const file = input.files?.[0]
    if (!file) return
    if (file.size > 512 * 1024) {
        toast.add({ title: 'Icon too large', description: 'Max 512 KB', color: 'red' })
        return
    }
    pendingIconFile.value = file
    // show local preview immediately
    form.value.icon_url = URL.createObjectURL(file)
    removeIcon.value = false
    if (fileInput.value) fileInput.value.value = ''
}

const queueRemoveIcon = () => {
    form.value.icon_url = null
    form.value.icon_key = null
    pendingIconFile.value = null
    removeIcon.value = true
}

onMounted(fetchSettings)
</script>


