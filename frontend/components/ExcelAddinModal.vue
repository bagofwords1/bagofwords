<template>
    <UCard>
        <!-- Header -->
        <template #header>
            <div class="flex items-center justify-between">
                <div class="flex items-center gap-3">
                    <img src="/data_sources_icons/excel.png" alt="Excel" class="w-6 h-6" />
                    <h3 class="text-lg font-semibold text-gray-900 dark:text-white">{{ $t('settings.integrations.channels.excel.title') }}</h3>
                </div>
                <UButton
                    color="gray"
                    variant="ghost"
                    icon="i-heroicons-x-mark-20-solid"
                    :title="$t('settings.integrations.channels.common.close')"
                    @click="$emit('close')"
                />
            </div>
            <p class="text-sm text-gray-500 dark:text-gray-400 mt-2">
                {{ $t('settings.integrations.channels.excel.subtitle') }}
            </p>
        </template>

        <div v-if="loading" class="py-12 flex items-center justify-center">
            <p class="text-sm text-gray-500 dark:text-gray-400">{{ $t('settings.integrations.channels.excel.loading') }}</p>
        </div>

        <div v-else class="space-y-5">
            <!-- Instructions -->
            <div>
                <div class="text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">{{ $t('settings.integrations.channels.excel.setup') }}</div>
                <ol class="text-sm text-gray-600 dark:text-gray-400 space-y-1.5 list-decimal list-inside">
                    <li>{{ $t('settings.integrations.channels.excel.step1') }}</li>
                    <li>{{ $t('settings.integrations.channels.excel.step2') }}</li>
                    <li>{{ $t('settings.integrations.channels.excel.step3') }}</li>
                    <i18n-t keypath="settings.integrations.channels.excel.step4" tag="li">
                        <template #manifest><code class="text-xs bg-gray-100 dark:bg-gray-800 px-1 py-0.5 rounded">manifest.xml</code></template>
                    </i18n-t>
                    <li>{{ $t('settings.integrations.channels.excel.step5') }}</li>
                </ol>
            </div>

            <!-- Manifest -->
            <div>
                <div class="text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">{{ $t('settings.integrations.channels.excel.manifest') }}</div>
                <div v-if="error" class="text-sm text-red-500 py-2">{{ error }}</div>
                <div v-else class="space-y-3">
                    <div class="flex items-center gap-2">
                        <UButton
                            size="xs"
                            color="blue"
                            @click="downloadManifest"
                        >
                            <UIcon name="heroicons-arrow-down-tray" class="w-3.5 h-3.5 me-1" />
                            {{ $t('settings.integrations.channels.excel.downloadManifest') }}
                        </UButton>
                        <button
                            @click="copyManifest"
                            class="flex items-center gap-1 px-2 py-1 rounded text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600 transition-colors"
                        >
                            <UIcon :name="copied ? 'heroicons-check' : 'heroicons-clipboard-document'" class="w-3.5 h-3.5" />
                            {{ copied ? $t('settings.integrations.channels.excel.copied') : $t('settings.integrations.channels.excel.copyXml') }}
                        </button>
                    </div>
                    <button
                        @click="showManifest = !showManifest"
                        class="flex items-center gap-2 text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 transition-colors"
                    >
                        <UIcon
                            :name="showManifest ? 'heroicons-chevron-down' : 'heroicons-chevron-right'"
                            class="w-3 h-3 rtl-flip"
                        />
                        {{ showManifest ? $t('settings.integrations.channels.excel.hideXml') : $t('settings.integrations.channels.excel.showXml') }}
                    </button>
                    <div v-if="showManifest" class="relative bg-gray-50 dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700">
                        <pre class="px-3 py-2.5 font-mono text-xs text-gray-700 dark:text-gray-300 max-h-64 overflow-auto">{{ manifestXml }}</pre>
                    </div>
                </div>
            </div>

            <!-- Tenant-wide deployment -->
            <div class="pt-2 border-t border-gray-100 dark:border-gray-800">
                <div class="text-[11px] uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">{{ $t('settings.integrations.channels.excel.tenantWide') }}</div>
                <p class="text-sm text-gray-600 dark:text-gray-400">
                    {{ $t('settings.integrations.channels.excel.tenantWideDesc') }}
                </p>
            </div>
        </div>
    </UCard>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'

defineEmits(['close'])

const { t } = useI18n()

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
        error.value = e.message || t('settings.integrations.channels.excel.failedLoadManifest')
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
