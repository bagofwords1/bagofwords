<template>
    <div class="mt-6">
        <h2 class="text-lg font-medium text-gray-900 dark:text-white">
            {{ $t('settings.pii.title') }}
            <p class="text-sm text-gray-500 dark:text-gray-400 font-normal mb-8 max-w-3xl">
                {{ $t('settings.pii.subtitle') }}
            </p>
        </h2>

        <!-- Enterprise gate (belt-and-suspenders; the tab is already hidden when unlicensed) -->
        <div v-if="!hasFeature('pii_protection')" class="max-w-2xl rounded-lg border border-amber-300 bg-amber-50 dark:bg-amber-950/40 dark:border-amber-800 p-5">
            <div class="flex items-center gap-2 font-medium text-amber-800 dark:text-amber-300">
                <Icon name="heroicons:sparkles" class="w-5 h-5" />
                {{ $t('settings.pii.enterpriseTitle') }}
            </div>
            <p class="text-sm text-amber-700 dark:text-amber-400 mt-2">{{ $t('settings.pii.enterpriseBody') }}</p>
        </div>

        <template v-else>
            <div v-if="loading" class="py-4"><ULoader /></div>

            <div v-else class="space-y-8 max-w-3xl">
                <!-- Master switch -->
                <div class="flex flex-col">
                    <div class="flex items-center justify-between">
                        <div class="font-medium flex items-center gap-2">
                            <Icon name="heroicons:shield-check" class="w-5 h-5 text-blue-600" />
                            {{ $t('settings.pii.masterLabel') }}
                        </div>
                        <UToggle v-model="pii.enabled" @update:model-value="save" />
                    </div>
                    <p class="text-sm text-gray-500 dark:text-gray-400 mt-2.5">{{ $t('settings.pii.masterDesc') }}</p>
                </div>

                <!-- Mode -->
                <div :class="{ 'opacity-50 pointer-events-none': !pii.enabled }">
                    <div class="font-medium mb-3">{{ $t('settings.pii.modeLabel') }}</div>
                    <div class="space-y-2">
                        <label class="flex items-start gap-3 cursor-pointer">
                            <input type="radio" value="replace" v-model="pii.mode" class="mt-1" @change="save" />
                            <div>
                                <div class="text-sm font-medium">{{ $t('settings.pii.modeReplace') }}</div>
                                <div class="text-xs text-gray-500 dark:text-gray-400">{{ $t('settings.pii.modeReplaceDesc') }}</div>
                            </div>
                        </label>
                        <label class="flex items-start gap-3 cursor-pointer">
                            <input type="radio" value="block" v-model="pii.mode" class="mt-1" @change="save" />
                            <div>
                                <div class="text-sm font-medium">{{ $t('settings.pii.modeBlock') }}</div>
                                <div class="text-xs text-gray-500 dark:text-gray-400">{{ $t('settings.pii.modeBlockDesc') }}</div>
                            </div>
                        </label>
                    </div>
                </div>

                <hr class="border-gray-200 dark:border-gray-700" />

                <!-- Built-in rules -->
                <div :class="{ 'opacity-50 pointer-events-none': !pii.enabled }">
                    <h3 class="text-base font-semibold">{{ $t('settings.pii.builtinTitle') }}</h3>
                    <p class="text-sm text-gray-500 dark:text-gray-400 mt-1 mb-4">{{ $t('settings.pii.builtinDesc') }}</p>
                    <div class="space-y-3">
                        <div v-for="rule in builtinRules" :key="rule.id"
                             class="flex items-center justify-between gap-4 border border-gray-200 dark:border-gray-700 rounded-md px-4 py-3">
                            <div class="min-w-0">
                                <div class="text-sm font-medium">{{ rule.name }}</div>
                                <div class="text-xs text-gray-400">{{ rule.pattern_count }} pattern{{ rule.pattern_count === 1 ? '' : 's' }}</div>
                            </div>
                            <div class="flex items-center gap-3 shrink-0">
                                <USelect
                                    :model-value="builtinAction(rule.id)"
                                    @update:model-value="setBuiltinAction(rule.id, $event); save()"
                                    :options="actionOptions" size="sm" class="w-28" />
                                <UInput
                                    v-if="builtinAction(rule.id) === 'replace'"
                                    :model-value="builtinReplacement(rule.id)"
                                    @update:model-value="setBuiltinReplacement(rule.id, $event)"
                                    @blur="save"
                                    size="sm" class="w-44" :placeholder="rule.replacement" />
                                <span v-else class="w-44 text-xs text-red-500 dark:text-red-400 text-right flex items-center justify-end gap-1">
                                    <Icon name="heroicons:no-symbol" class="w-3.5 h-3.5" />
                                    {{ $t('settings.pii.blocksRequest') }}
                                </span>
                                <UToggle
                                    :model-value="builtinEnabled(rule.id)"
                                    @update:model-value="setBuiltinEnabled(rule.id, $event); save()" />
                            </div>
                        </div>
                    </div>
                </div>

                <hr class="border-gray-200 dark:border-gray-700" />

                <!-- Custom rules -->
                <div :class="{ 'opacity-50 pointer-events-none': !pii.enabled }">
                    <div class="flex items-center justify-between">
                        <div>
                            <h3 class="text-base font-semibold">{{ $t('settings.pii.customTitle') }}</h3>
                            <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">{{ $t('settings.pii.customDesc') }}</p>
                        </div>
                        <UButton size="sm" icon="i-heroicons-plus" @click="addRule">{{ $t('settings.pii.addRule') }}</UButton>
                    </div>

                    <p v-if="pii.custom_rules.length === 0" class="text-sm text-gray-400 mt-4">{{ $t('settings.pii.noCustomRules') }}</p>

                    <div v-for="(rule, ri) in pii.custom_rules" :key="rule.id"
                         class="border border-gray-200 dark:border-gray-700 rounded-md p-4 mt-4 space-y-3">
                        <div class="flex items-center justify-between gap-3">
                            <div class="flex items-center gap-3 flex-1">
                                <UToggle v-model="rule.enabled" @update:model-value="save" />
                                <UInput v-model="rule.name" :placeholder="$t('settings.pii.ruleNamePlaceholder')"
                                        size="sm" class="flex-1" @blur="save" />
                            </div>
                            <UButton color="red" variant="ghost" size="sm" icon="i-heroicons-trash"
                                     @click="removeRule(ri)">{{ $t('settings.pii.removeRule') }}</UButton>
                        </div>

                        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
                            <div>
                                <label class="text-xs text-gray-500 dark:text-gray-400">{{ $t('settings.pii.action') }}</label>
                                <USelect
                                    :model-value="customAction(rule)"
                                    @update:model-value="rule.action = $event; save()"
                                    :options="actionOptions" size="sm" />
                            </div>
                            <div v-if="customAction(rule) === 'replace'">
                                <label class="text-xs text-gray-500 dark:text-gray-400">{{ $t('settings.pii.replacement') }}</label>
                                <UInput v-model="rule.replacement" size="sm" placeholder="[REDACTED]" @blur="save" />
                            </div>
                            <div v-else class="flex items-end">
                                <span class="text-xs text-red-500 dark:text-red-400 flex items-center gap-1 pb-2">
                                    <Icon name="heroicons:no-symbol" class="w-3.5 h-3.5" />
                                    {{ $t('settings.pii.blocksRequest') }}
                                </span>
                            </div>
                        </div>

                        <div>
                            <label class="text-xs text-gray-500 dark:text-gray-400">{{ $t('settings.pii.patterns') }}</label>
                            <div v-for="(pat, pi) in rule.patterns" :key="pi" class="flex items-center gap-2 mt-1.5">
                                <UInput v-model="rule.patterns[pi]" size="sm"
                                        class="flex-1 font-mono" :placeholder="$t('settings.pii.patternPlaceholder')" @blur="save" />
                                <UButton color="gray" variant="ghost" size="xs" icon="i-heroicons-x-mark"
                                         @click="removePattern(rule, pi)" />
                            </div>
                            <UButton variant="link" size="xs" icon="i-heroicons-plus" class="mt-1"
                                     @click="rule.patterns.push('')">{{ $t('settings.pii.addPattern') }}</UButton>
                        </div>
                    </div>
                </div>

                <hr class="border-gray-200 dark:border-gray-700" />

                <!-- Live test -->
                <div>
                    <h3 class="text-base font-semibold">{{ $t('settings.pii.testTitle') }}</h3>
                    <p class="text-sm text-gray-500 dark:text-gray-400 mt-1 mb-3">{{ $t('settings.pii.testDesc') }}</p>
                    <UTextarea v-model="testInput" :rows="4" :placeholder="$t('settings.pii.testPlaceholder')" class="font-mono" />
                    <div class="mt-2">
                        <UButton size="sm" color="gray" icon="i-heroicons-play" :loading="testing" @click="runTest">
                            {{ $t('settings.pii.testButton') }}
                        </UButton>
                    </div>

                    <div v-if="testDone" class="mt-4">
                        <div v-if="testResult.blocked" class="rounded-md border border-red-300 bg-red-50 dark:bg-red-950/40 dark:border-red-800 p-3 text-sm text-red-700 dark:text-red-300 flex items-center gap-2">
                            <Icon name="heroicons:no-symbol" class="w-4 h-4" />
                            {{ $t('settings.pii.testBlocked') }}
                        </div>
                        <template v-else>
                            <div class="text-xs text-gray-500 dark:text-gray-400 mb-1">{{ $t('settings.pii.testResult') }}</div>
                            <pre class="rounded-md border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 p-3 text-sm whitespace-pre-wrap break-words">{{ testResult.text }}</pre>
                        </template>
                        <p v-if="testResult.matches && testResult.matches.length" class="text-xs text-gray-500 dark:text-gray-400 mt-2">
                            {{ $t('settings.pii.testMatches', { rules: matchLabel }) }}
                        </p>
                        <p v-else-if="!testResult.blocked" class="text-xs text-green-600 mt-2">{{ $t('settings.pii.testNoMatches') }}</p>
                    </div>
                </div>
            </div>
        </template>
    </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'

definePageMeta({ auth: true, permissions: ['manage_settings'], layout: 'settings' })

const { t } = useI18n()
const toast = useToast()
const { hasFeature } = useEnterprise()

const loading = ref(true)
const testing = ref(false)
const testDone = ref(false)
const testInput = ref('')
const testResult = ref<{ text: string; matches: any[]; blocked: boolean }>({ text: '', matches: [], blocked: false })

interface CustomRule { id: string; name: string; patterns: string[]; replacement: string; enabled: boolean; action?: string | null }
const pii = reactive<{ enabled: boolean; mode: string; builtin_overrides: Record<string, any>; custom_rules: CustomRule[] }>({
    enabled: false,
    mode: 'replace',
    builtin_overrides: {},
    custom_rules: [],
})
const builtinRules = ref<Array<{ id: string; name: string; replacement: string; pattern_count: number }>>([])

const actionOptions = computed(() => [
    { label: t('settings.pii.actionReplace'), value: 'replace' },
    { label: t('settings.pii.actionBlock'), value: 'block' },
])

const matchLabel = computed(() =>
    (testResult.value.matches || []).map((m: any) => m.name + (m.count ? ` (${m.count})` : '')).join(', ')
)

// --- built-in override helpers (effective enable/replacement/action) ---
const builtinEnabled = (id: string) => pii.builtin_overrides[id]?.enabled ?? true
const builtinReplacement = (id: string) => {
    const ov = pii.builtin_overrides[id]?.replacement
    if (ov) return ov
    const def = builtinRules.value.find(r => r.id === id)
    return def?.replacement || ''
}
// Effective action = per-rule override, else the workspace default (pii.mode).
const builtinAction = (id: string) => pii.builtin_overrides[id]?.action || pii.mode
const customAction = (rule: CustomRule) => rule.action || pii.mode
const setBuiltinEnabled = (id: string, val: boolean) => {
    pii.builtin_overrides[id] = { ...(pii.builtin_overrides[id] || {}), enabled: val }
}
const setBuiltinReplacement = (id: string, val: string) => {
    pii.builtin_overrides[id] = { ...(pii.builtin_overrides[id] || {}), replacement: val }
}
const setBuiltinAction = (id: string, val: string) => {
    pii.builtin_overrides[id] = { ...(pii.builtin_overrides[id] || {}), action: val }
}

// --- custom rule editing ---
const addRule = () => {
    pii.custom_rules.push({
        id: `custom-${Date.now()}-${Math.floor(Math.random() * 1000)}`,
        name: '', patterns: [''], replacement: '[REDACTED]', enabled: true, action: pii.mode,
    })
}
const removeRule = (i: number) => { pii.custom_rules.splice(i, 1); save() }
const removePattern = (rule: CustomRule, i: number) => {
    rule.patterns.splice(i, 1)
    if (rule.patterns.length === 0) rule.patterns.push('')
    save()
}

const buildConfig = () => ({
    enabled: pii.enabled,
    mode: pii.mode,
    builtin_overrides: pii.builtin_overrides,
    custom_rules: pii.custom_rules
        .map(r => ({ ...r, patterns: r.patterns.map(p => p.trim()).filter(Boolean) }))
        .filter(r => r.name.trim() || r.patterns.length),
})

const load = async () => {
    loading.value = true
    try {
        const [settingsRes, rulesRes] = await Promise.all([
            useMyFetch('/api/organization/settings'),
            useMyFetch('/api/organization/pii/builtin-rules'),
        ])
        const cfg = (settingsRes.data.value as any)?.config?.pii_protection
        if (cfg) {
            pii.enabled = !!cfg.enabled
            pii.mode = cfg.mode || 'replace'
            pii.builtin_overrides = cfg.builtin_overrides || {}
            pii.custom_rules = (cfg.custom_rules || []).map((r: any) => ({
                id: r.id || `custom-${Math.random()}`,
                name: r.name || '',
                patterns: r.patterns && r.patterns.length ? [...r.patterns] : [''],
                replacement: r.replacement || '[REDACTED]',
                enabled: r.enabled !== false,
                action: r.action || null,
            }))
        }
        builtinRules.value = (rulesRes.data.value as any)?.rules || []
    } finally {
        loading.value = false
    }
}

let saving = false
const save = async () => {
    if (saving) return
    saving = true
    try {
        const res = await useMyFetch('/api/organization/settings', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ config: { pii_protection: buildConfig() } }),
        })
        if (res.status.value !== 'success') {
            const detail = (res.error?.value as any)?.data?.detail || t('settings.pii.saveError')
            throw new Error(detail)
        }
        toast.add({ title: t('settings.pii.saved'), color: 'green', timeout: 2000 })
    } catch (e: any) {
        toast.add({ title: t('settings.pii.saveError'), description: e.message, color: 'red', timeout: 5000, icon: 'i-heroicons-exclamation-circle' })
    } finally {
        saving = false
    }
}

const runTest = async () => {
    testing.value = true
    testDone.value = false
    try {
        const res = await useMyFetch('/api/organization/pii/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text: testInput.value, config: buildConfig() }),
        })
        if (res.status.value !== 'success') {
            const detail = (res.error?.value as any)?.data?.detail || 'Test failed'
            throw new Error(detail)
        }
        testResult.value = res.data.value as any
        testDone.value = true
    } catch (e: any) {
        toast.add({ title: 'Test failed', description: e.message, color: 'red', timeout: 5000, icon: 'i-heroicons-exclamation-circle' })
    } finally {
        testing.value = false
    }
}

onMounted(load)
</script>
