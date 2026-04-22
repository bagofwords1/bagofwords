<template>
    <UCard>
        <!-- Header -->
        <template #header>
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-3">
                    <img src="/data_sources_icons/excel.png" alt="Excel" class="w-6 h-6" />
                    <h3 class="text-lg font-semibold text-gray-900">Excel Add-in</h3>
                </div>
                <UButton
                    color="gray"
                    variant="ghost"
                    icon="i-heroicons-x-mark-20-solid"
                    @click="$emit('close')"
                />
            </div>
            <p class="text-sm text-gray-500 mt-2">
                Sideload the Bag of Words add-in directly from this instance.
            </p>
        </template>

        <div v-if="loading" class="py-12 flex items-center justify-center">
            <p class="text-sm text-gray-500">Loading...</p>
        </div>

        <div v-else class="space-y-5">
            <!-- Instructions -->
            <div>
                <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-2">Setup</div>
                <ol class="text-sm text-gray-600 space-y-1.5 list-decimal list-inside">
                    <li>Download the manifest file below</li>
                    <li>Open <strong>Excel</strong> (desktop or web)</li>
                    <li>Go to <strong>Home</strong> tab &rarr; <strong>Add-ins</strong> &rarr; <strong>More Add-ins</strong></li>
                    <li>Click <strong>Upload My Add-in</strong> and upload the <code class="text-xs bg-gray-100 px-1 py-0.5 rounded">manifest.xml</code></li>
                    <li>The <strong>BOW</strong> button will appear in the Home tab</li>
                </ol>
            </div>

            <!-- Manifest -->
            <div>
                <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-2">Manifest</div>
                <div v-if="error" class="text-sm text-red-500 py-2">{{ error }}</div>
                <div v-else class="space-y-3">
                    <div class="flex items-center gap-2">
                        <UButton
                            size="xs"
                            color="blue"
                            @click="downloadManifest"
                        >
                            <UIcon name="heroicons-arrow-down-tray" class="w-3.5 h-3.5 me-1" />
                            Download manifest.xml
                        </UButton>
                        <button
                            @click="copyManifest"
                            class="flex items-center gap-1 px-2 py-1 rounded text-xs text-gray-500 hover:text-gray-700 hover:bg-gray-200 transition-colors"
                        >
                            <UIcon :name="copied ? 'heroicons-check' : 'heroicons-clipboard-document'" class="w-3.5 h-3.5" />
                            {{ copied ? 'Copied' : 'Copy XML' }}
                        </button>
                    </div>
                    <button
                        @click="showManifest = !showManifest"
                        class="flex items-center gap-2 text-xs text-gray-400 hover:text-gray-600 transition-colors"
                    >
                        <UIcon
                            :name="showManifest ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
                            class="w-3 h-3 rtl-flip"
                        />
                        {{ showManifest ? 'Hide XML' : 'Show XML' }}
                    </button>
                    <div v-if="showManifest" class="relative bg-gray-50 rounded-lg border border-gray-200">
                        <pre class="px-3 py-2.5 font-mono text-xs text-gray-700 max-h-64 overflow-auto">{{ manifestXml }}</pre>
                    </div>
                </div>
            </div>

            <!-- Tenant-wide deployment -->
            <div class="pt-2 border-t border-gray-100">
                <div class="text-[11px] uppercase tracking-wide text-gray-500 mb-2">Tenant-wide deployment</div>
                <p class="text-sm text-gray-600">
                    For organization-wide rollout, your Microsoft 365 admin can upload this manifest via
                    <strong>Admin Center</strong> &rarr; <strong>Settings</strong> &rarr; <strong>Integrated apps</strong>.
                </p>
            </div>
        </div>
    </UCard>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'

defineEmits(['close'])

const manifestXml = ref('')
const loading = ref(true)
const error = ref('')
const copied = ref(false)
const showManifest = ref(false)

async function fetchManifest() {
    try {
        const res = await fetch(`${window.location.origin}/excel/manifest.xml`)
        if (!res.ok) throw new Error(`Failed to load manifest (${res.status})`)
        manifestXml.value = await res.text()
    } catch (e: any) {
        error.value = e.message || 'Failed to load manifest'
    } finally {
        loading.value = false
    }
}

async function copyManifest() {
    try {
        await navigator.clipboard.writeText(manifestXml.value)
        copied.value = true
        setTimeout(() => { copied.value = false }, 2000)
    } catch {
        // Fallback
    }
}

function downloadManifest() {
    const blob = new Blob([manifestXml.value], { type: 'application/xml' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'manifest.xml'
    a.click()
    URL.revokeObjectURL(url)
}

onMounted(fetchManifest)
</script>
