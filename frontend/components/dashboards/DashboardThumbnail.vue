<template>
    <img v-if="src" :src="src" :alt="alt" @error="fail" />
</template>

<script setup lang="ts">
// A dashboard card's thumbnail. A page's (/thumbnails/...) is a public file
// the browser loads itself; a deck's is its first slide preview, served by
// the preview routes, which need the reader's token -- an <img src> cannot
// send it, so that one is fetched with it and shown as a blob URL. Emits
// `error` when the image cannot be loaded, so the card shows its icon.
const props = defineProps<{ url: string; alt?: string }>()
const emit = defineEmits<{ (e: 'error'): void }>()

const config = useRuntimeConfig()
const token = useApiAuthorization()
const { organization } = useOrganization()

const src = ref<string | null>(null)
let blobUrl: string | null = null
let current = 0

function release() {
    if (blobUrl) URL.revokeObjectURL(blobUrl)
    blobUrl = null
}

function fail() {
    src.value = null
    emit('error')
}

async function load(url: string) {
    const request = ++current
    release()
    src.value = null
    const full = /^https?:/.test(url) ? url : `${config.public.baseURL}${url}`
    if (url.startsWith('/thumbnails/') || /^https?:/.test(url)) {
        src.value = full
        return
    }
    try {
        const headers: Record<string, string> = {}
        if (token.value) headers.Authorization = token.value
        if (organization.value?.id) headers['X-Organization-Id'] = organization.value.id
        const response = await fetch(full, { headers })
        if (!response.ok) throw new Error(String(response.status))
        const blob = await response.blob()
        if (request !== current) return
        blobUrl = URL.createObjectURL(blob)
        src.value = blobUrl
    } catch {
        if (request === current) fail()
    }
}

watch(() => props.url, url => { load(url) }, { immediate: true })
onBeforeUnmount(() => { current++; release() })
</script>
