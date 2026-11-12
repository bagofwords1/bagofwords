<template>
    <div>
        <!-- Enterprise gate (belt-and-suspenders; the tab is already hidden when unlicensed) -->
        <div v-if="!hasFeature('pii_protection')" class="rounded-lg border border-amber-200 bg-amber-50/70 dark:bg-amber-950/40 dark:border-amber-900/60 p-5">
            <div class="flex items-center gap-2 text-sm font-semibold text-amber-800 dark:text-amber-300">
                <Icon name="heroicons:sparkles" class="w-4 h-4" />
                {{ $t('settings.pii.enterpriseTitle') }}
            </div>
            <p class="text-sm text-amber-800/90 dark:text-amber-300/90 mt-1.5">{{ $t('settings.pii.enterpriseBody') }}</p>
        </div>

        <template v-else>
            <SettingsLoading v-if="loading" />

            <div v-else>
                <!-- Master switch + default action -->
                <SettingsSection>
                    <SettingsRow :label="$t('settings.pii.masterLabel')" :description="$t('settings.pii.masterDesc')">
                        <UToggle v-model="pii.enabled" @update:model-value="save" />
                    </SettingsRow>

                    <fieldset class="mt-6" :disabled="!pii.enabled" :class="{ 'opacity-50': !pii.enabled }">
                        <legend class="text-sm font-medium text-ink mb-3">{{ $t('settings.pii.modeLabel') }}</legend>
                        <div class="grid gap-3 sm:grid-cols-2">
                            <label
                                v-for="opt in modeOptions"
                                :key="opt.value"
                                :class="[
                                    'flex items-start gap-3 rounded-lg border px-4 py-3 cursor-pointer transition-colors',
                                    pii.mode === opt.value
                                        ? 'border-blue-500 bg-blue-50/60 dark:border-blue-400 dark:bg-blue-950/30'
                                        : 'border-line hover:border-line-strong'
                                ]"
                            >
                                <input type="radio" :value="opt.value" v-model="pii.mode" class="mt-0.5 accent-blue-600" @change="save" />
                                <span class="min-w-0">
                                    <span class="block text-sm font-medium text-ink">{{ opt.label }}</span>
                                    <span class="block mt-0.5 text-xs text-ink-muted leading-relaxed">{{ opt.description }}</span>
                                </span>
                            </label>
                        </div>
                    </fieldset>
                </SettingsSection>

                <!-- Built-in rules -->
                <SettingsSection :title="$t('settings.pii.builtinTitle')" :description="$t('settings.pii.builtinDesc')">
                    <div :class="['bow-table-card divide-y divide-line-subtle', { 'opacity-50 pointer-events-none': !pii.enabled }]">
                        <div v-for="rule in builtinRules" :key="rule.id" class="flex items-center justify-between gap-4 px-5 py-3">
                            <div class="min-w-0">
                                <div class="text-sm font-medium text-ink">{{ rule.name }}</div>
                                <div class="text-xs text-ink-muted">{{ $t('settings.pii.patternCount', { n: rule.pattern_count }, rule.pattern_count) }}</div>
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
                                    size="sm" class="w-44" input-class="font-mono text-xs" :placeholder="rule.replacement" />
                                <span v-else class="w-44 text-xs text-red-600 dark:text-red-400 flex items-center justify-end gap-1">
                                    <Icon name="heroicons:no-symbol" class="w-3.5 h-3.5" />
                                    {{ $t('settings.pii.blocksRequest') }}
                                </span>
                                <UToggle
                                    :model-value="builtinEnabled(rule.id)"
                                    @update:model-value="setBuiltinEnabled(rule.id, $event); save()" />
                            </div>
                        </div>
                    </div>
                </SettingsSection>

                <!-- Custom rules -->
                <SettingsSection :title="$t('settings.pii.customTitle')" :description="$t('settings.pii.customDesc')">
                    <template #actions>
                        <UButton size="sm" color="gray" icon="i-heroicons-plus" :disabled="!pii.enabled" @click="addRule">{{ $t('settings.pii.addRule') }}</UButton>
                    </template>

                    <div :class="{ 'opacity-50 pointer-events-none': !pii.enabled }">
                        <div v-if="pii.custom_rules.length === 0" class="rounded-lg border border-dashed border-line">
                            <SettingsEmpty icon="i-heroicons-funnel" :title="$t('settings.pii.noCustomRules')" compact />
                        </div>

                        <div v-for="(rule, ri) in pii.custom_rules" :key="rule.id"
                             class="rounded-lg border border-line p-4 space-y-4 [&+&]:mt-3">
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
                                    <label class="block mb-1 text-xs font-medium text-ink-muted">{{ $t('settings.pii.action') }}</label>
                                    <USelect
                                        :model-value="customAction(rule)"
                                        @update:model-value="rule.action = $event; save()"
                                        :options="actionOptions" size="sm" />
                                </div>
                                <div v-if="customAction(rule) === 'replace'">
                                    <label class="block mb-1 text-xs font-medium text-ink-muted">{{ $t('settings.pii.replacement') }}</label>
                                    <UInput v-model="rule.replacement" size="sm" input-class="font-mono text-xs" placeholder="[REDACTED]" @blur="save" />
                                </div>
                                <div v-else class="flex items-end">
                                    <span class="text-xs text-red-600 dark:text-red-400 flex items-center gap-1 pb-2">
                                        <Icon name="heroicons:no-symbol" class="w-3.5 h-3.5" />
                                        {{ $t('settings.pii.blocksRequest') }}
                                    </span>
                                </div>
                            </div>

                            <div>
                                <label class="block text-xs font-medium text-ink-muted">{{ $t('settings.pii.patterns') }}</label>
                                <div v-for="(pat, pi) in rule.patterns" :key="pi" class="flex items-center gap-2 mt-1.5">
                                    <UInput v-model="rule.patterns[pi]" size="sm"
                                            class="flex-1" input-class="font-mono text-xs" :placeholder="$t('settings.pii.patternPlaceholder')" @blur="save" />
                                    <UButton color="gray" variant="ghost" size="xs" icon="i-heroicons-x-mark"
                                             :aria-label="$t('common.remove')" @click="removePattern(rule, pi)" />
                                </div>
                                <UButton variant="link" size="xs" icon="i-heroicons-plus" class="mt-1 -ms-2"
                                         @click="rule.patterns.push('')">{{ $t('settings.pii.addPattern') }}</UButton>
                            </div>
                        </div>
                    </div>
                </SettingsSection>

                <!-- Live test -->
                <SettingsSection :title="$t('settings.pii.testTitle')" :description="$t('settings.pii.testDesc')">
                    <UTextarea v-model="testInput" :rows="4" :placeholder="$t('settings.pii.testPlaceholder')" textarea-class="font-mono text-xs" />
                    <div class="mt-3">
                        <UButton size="sm" color="gray" icon="i-heroicons-play" :loading="testing" @click="runTest">
                            {{ $t('settings.pii.testButton') }}
                        </UButton>
                    </div>

                    <div v-if="testDone" class="mt-4">
                        <div v-if="testResult.blocked" class="rounded-lg border border-red-200 bg-red-50 dark:bg-red-950/40 dark:border-red-900/60 px-4 py-3 text-sm text-red-700 dark:text-red-300 flex items-center gap-2">
                            <Icon name="heroicons:no-symbol" class="w-4 h-4" />
                            {{ $t('settings.pii.testBlocked') }}
                        </div>
                        <template v-else>
                            <div class="text-xs font-medium text-ink-muted mb-1.5">{{ $t('settings.pii.testResult') }}</div>
                            <pre class="rounded-lg border border-line bg-surface-subtle px-4 py-3 font-mono text-xs text-ink-2 whitespace-pre-wrap break-words">{{ testResult.text }}</pre>
                        </template>
                        <p v-if="testResult.matches && testResult.matches.length" class="text-xs text-ink-muted mt-2">
                            {{ $t('settings.pii.testMatches', { rules: matchLabel }) }}
                        </p>
                        <p v-else-if="!testResult.blocked" class="text-xs text-green-700 dark:text-green-400 mt-2">{{ $t('settings.pii.testNoMatches') }}</p>
                    </div>
                </SettingsSection>
            </div>
        </template>
    </div>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import SettingsSection from '~/components/settings/SettingsSection.vue'
import SettingsRow from '~/components/settings/SettingsRow.vue'
import SettingsEmpty from '~/components/settings/SettingsEmpty.vue'
import SettingsLoading from '~/components/settings/SettingsLoading.vue'

definePageMeta({ auth: true, permissions: ['manage_settings'] })

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

const modeOptions = computed(() => [
    { value: 'replace', label: t('settings.pii.modeReplace'), description: t('settings.pii.modeReplaceDesc') },
    { value: 'block', label: t('settings.pii.modeBlock'), description: t('settings.pii.modeBlockDesc') },
])

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
