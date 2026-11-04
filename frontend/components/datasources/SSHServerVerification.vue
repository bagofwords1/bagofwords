<template>
  <section class="rounded-lg border border-line bg-surface-subtle p-3 space-y-3" aria-labelledby="ssh-verification-title">
    <h3 id="ssh-verification-title" class="text-sm font-medium text-ink">{{ $t('data.sshSetup.title') }}</h3>
    <p class="text-xs text-ink-muted">{{ $t('data.sshSetup.hint') }}</p>
    <div v-if="modelValue" class="space-y-1">
      <p class="text-sm text-ink">{{ $t('data.sshSetup.saved') }}</p>
      <code v-if="savedFingerprint" dir="ltr" class="block break-all text-xs text-ink-2">{{ savedFingerprint }}</code>
    </div>
    <UButton color="gray" variant="soft" :loading="fetching" :disabled="!host || !port || fetching" @click="fetchKeys">{{ $t('data.sshSetup.fetch') }}</UButton>
    <div v-if="candidates.length" class="space-y-3">
      <label for="ssh-key-candidate" class="block text-xs text-ink">{{ $t('data.sshSetup.fingerprint') }}</label>
      <select id="ssh-key-candidate" v-model="selectedKey" dir="ltr" class="w-full rounded-lg border border-line bg-surface p-2 text-xs text-ink">
        <option v-for="key in candidates" :key="key.public_key" :value="key.public_key">{{ key.algorithm }} · {{ key.fingerprint }}</option>
      </select>
      <UCheckbox v-model="confirmed" :label="$t('data.sshSetup.confirm')" />
      <UButton color="blue" :disabled="!confirmed || !selectedKey" @click="trustKey">{{ $t('data.sshSetup.trust') }}</UButton>
    </div>
    <p v-if="error" role="alert" class="text-xs text-red-600">{{ error }}</p>
  </section>
</template>

<script setup lang="ts">
type Candidate = { public_key: string; fingerprint: string; algorithm: string }
const props = defineProps<{ host?: string; port?: number; modelValue?: string; connectionId?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()
const { t } = useI18n()
const { getErrorMessage } = useErrorMessage()
const candidates = ref<Candidate[]>([])
const selectedKey = ref('')
const confirmed = ref(false)
const fetching = ref(false)
const error = ref('')
const savedFingerprint = ref('')
let generation = 0
let candidateTarget = ''
const target = () => JSON.stringify([props.host, props.port || 22])
watch(selectedKey, () => { confirmed.value = false })
watch(() => [props.host, props.port], (now, before) => {
  generation++
  candidates.value = []
  confirmed.value = false
  selectedKey.value = ''
  fetching.value = false
  error.value = ''
  if (before?.[0] && (now[0] !== before[0] || now[1] !== before[1])) emit('update:modelValue', '')
})
watch(() => props.modelValue, async (value) => {
  savedFingerprint.value = ''
  if (!value) return
  try {
    const blob = Uint8Array.from(atob(value.trim().split(/\s+/)[1]), c => c.charCodeAt(0))
    const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', blob))
    if (props.modelValue === value) savedFingerprint.value = 'SHA256:' + btoa(String.fromCharCode(...digest)).replace(/=+$/, '')
  } catch { /* Manual entry is validated when testing the connection. */ }
}, { immediate: true })
onBeforeUnmount(() => { generation++ })
async function fetchKeys() {
  if (fetching.value || !props.host) return
  const requestGeneration = ++generation
  const requestTarget = target()
  fetching.value = true
  error.value = ''
  candidates.value = []
  confirmed.value = false
  try {
    const endpoint = props.connectionId ? `/connections/${encodeURIComponent(props.connectionId)}/ssh/host-keys` : '/connections/ssh/host-keys'
    const response = await useMyFetch(endpoint, {
      method: 'POST', body: { host: props.host, port: props.port || 22 },
    })
    if (generation !== requestGeneration || target() !== requestTarget) return
    if (response.error.value) throw response.error.value
    const data = response.data.value as any
    if (!data?.keys?.length || data.host !== props.host || data.port !== (props.port || 22)) throw new Error('No matching server keys')
    candidates.value = data.keys
    selectedKey.value = data.keys[0].public_key
    candidateTarget = requestTarget
  } catch (err) {
    if (generation === requestGeneration) error.value = getErrorMessage(err, t('data.sshSetup.fetchFailed'))
  } finally {
    if (generation === requestGeneration) fetching.value = false
  }
}
function trustKey() {
  if (!confirmed.value || target() !== candidateTarget || !candidates.value.some(k => k.public_key === selectedKey.value)) return
  emit('update:modelValue', selectedKey.value)
  candidates.value = []
  confirmed.value = false
}
</script>
