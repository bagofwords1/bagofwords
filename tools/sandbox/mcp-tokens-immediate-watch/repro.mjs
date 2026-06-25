// Reproduction for the "MCP modal always shows 0 tokens" bug.
//
// The modal loads its API-key list from `watch(isOpen, cb)`. When the modal is
// mounted with `v-if="show"` while `show` is already true (default.vue sidebar),
// `isOpen` is true on the first render, so a NON-immediate watch never fires and
// loadApiKeys() never runs -> apiKeys stays [] -> "0 tokens".
//
// This mirrors the component's reactive contract with Vue directly (no DOM).
import { ref, computed, watch, nextTick } from 'vue'

// Simulate one mount of the modal. `initiallyOpen` models the v-if-already-open
// mount (modelValue is true from the first setup run). `immediate` is the watcher
// option under test. `loadCount()` returns how many times the open-handler
// (loadApiKeys) has run so far.
async function mountModal({ initiallyOpen, immediate }) {
  const modelValue = ref(initiallyOpen)
  const isOpen = computed(() => modelValue.value)
  let count = 0
  watch(isOpen, (open) => { if (open) count++ }, { immediate })
  await nextTick()
  return { loadCount: () => count, open: async () => { modelValue.value = true; await nextTick() } }
}

// Case 1: v-if already-open mount (default.vue). Modal mounts with isOpen=true
// and is torn down on close, so isOpen never transitions during its lifetime.
const brokenVIf = await mountModal({ initiallyOpen: true, immediate: false })
const fixedVIf  = await mountModal({ initiallyOpen: true, immediate: true })

// Case 2: always-mounted toggle (index.vue). modelValue starts false, then opens.
const toggle = await mountModal({ initiallyOpen: false, immediate: false })
await toggle.open()

const b = brokenVIf.loadCount(), f = fixedVIf.loadCount(), tg = toggle.loadCount()
console.log(`[v-if already-open mount]  broken(non-immediate): loadCount=${b}   <-- BUG: keys never load`)
console.log(`[v-if already-open mount]  fixed(immediate):      loadCount=${f}   <-- FIX: keys load`)
console.log(`[always-mounted toggle]    broken(non-immediate): loadCount=${tg}   (index.vue path already works)`)

const discriminates = b === 0 && f === 1 && tg === 1
console.log(`discriminates: ${discriminates}`)

if (!discriminates) {
  console.error('FAIL: repro did not discriminate as expected')
  process.exit(1)
}
console.log('PASS')
