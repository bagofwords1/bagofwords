<template>
    <div v-if="visibleItems.length > 0" class="flex flex-wrap justify-center gap-3">
        <button
            v-for="(item, idx) in visibleItems"
            :key="item.key + '-' + idx"
            @click="emitContent(item.value)"
            :class="['group relative overflow-hidden rounded-2xl bg-white px-4 py-2 text-xs text-gray-800 font-medium transition-all duration-300 ease-out hover:bg-gradient-to-r hover:from-gray-50 hover:to-gray-100 border border-gray-200', (fadingIndex === idx) ? 'swap-out' : 'swap-in']">
            <span class="ease absolute right-0 -mt-12 h-32 w-8 translate-x-12 rotate-12 transform bg-white opacity-10 transition-all duration-700 group-hover:-translate-x-40"></span>
            <span class="relative">{{ item.label }}</span>
        </button>
    </div>
    <div v-else-if="shouldShowSpinner" class="flex items-center justify-center">
        <Spinner class="h-4 w-4 text-gray-400" />
    </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onUnmounted, onMounted, nextTick } from 'vue'
import Spinner from '@/components/Spinner.vue'

const props = defineProps<{
    data_sources: any[]
}>()

const emit = defineEmits(['update-content'])

type Suggestion = { key: string, label: string, value: string }

// Build a flat pool of suggestions from all selected data sources
const pool = computed<Suggestion[]>(() => {
    if (!props.data_sources || !Array.isArray(props.data_sources)) return []
    const uniqueByLabel = new Map<string, Suggestion>()
    for (const ds of props.data_sources) {
        const starters = Array.isArray(ds?.conversation_starters) ? ds.conversation_starters : []
        for (const raw of starters) {
            const label = String(raw ?? '').split('\n')[0].trim()
            if (!label) continue
            if (!uniqueByLabel.has(label)) {
                uniqueByLabel.set(label, { key: `${label}-${uniqueByLabel.size}`, label, value: String(raw) })
            }
        }
    }
    return Array.from(uniqueByLabel.values())
})

const VISIBLE_COUNT = 5
const visibleItems = ref<Suggestion[]>([])
const fadingIndex = ref<number | null>(null)

function pickRandom<T>(arr: T[]): T | undefined {
    if (!arr.length) return undefined
    const index = Math.floor(Math.random() * arr.length)
    return arr[index]
}

function repopulateInitial() {
    const available = [...pool.value]
    const next: Suggestion[] = []
    const desired = Math.min(VISIBLE_COUNT, available.length)
    while (available.length > 0 && next.length < desired) {
        const candidate = pickRandom(available)
        if (!candidate) break
        next.push(candidate)
        const idx = available.indexOf(candidate)
        if (idx >= 0) available.splice(idx, 1)
    }
    visibleItems.value = next
}

function rotateOne() {
    const currentLabels = new Set(visibleItems.value.map(i => i.label))
    const candidates = pool.value.filter(i => !currentLabels.has(i.label))
    if (visibleItems.value.length === 0) return
    // If no unique candidates remain, stop rotating
    if (candidates.length === 0) return
    const replaceIndex = Math.floor(Math.random() * visibleItems.value.length)
    const replacement = pickRandom(candidates)
    if (!replacement) return
    fadingIndex.value = replaceIndex
    setTimeout(async () => {
        const nextArr = visibleItems.value.slice()
        nextArr.splice(replaceIndex, 1, replacement)
        visibleItems.value = nextArr
        await nextTick()
        setTimeout(() => { fadingIndex.value = null }, 50)
    }, 200)
}

let rotationInterval: any
function startRotation() {
    clearInterval(rotationInterval)
    rotationInterval = setInterval(() => {
        rotateOne()
    }, 5000) // rotate one every 5s
}

watch(pool, () => {
    repopulateInitial()
    startRotation()
}, { immediate: true })

onMounted(() => {})

onUnmounted(() => {
    clearInterval(rotationInterval)
})

function emitContent(content: string) {
    emit('update-content', content)
}

// Show spinner only while DS list is loading (undefined/null). Empty array shows nothing.
const shouldShowSpinner = computed(() => props.data_sources === undefined || props.data_sources === null)
</script>

<style scoped>
.fade-enter-active,
.fade-leave-active {
    transition: opacity 0.35s ease, transform 0.35s ease;
}

.fade-enter-from,
.fade-leave-to {
    opacity: 0;
}

/* Per-item swap animation without removing element from flow */
.swap-in {
    opacity: 1;
    transform: translateY(0);
    transition: opacity 0.25s ease, transform 0.25s ease;
}
.swap-out {
    opacity: 0;
    transform: translateY(4px);
    transition: opacity 0.25s ease, transform 0.25s ease;
}
</style>