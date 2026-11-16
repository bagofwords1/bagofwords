<template>
    <div class="space-y-3" data-testid="frame-ancestors-editor">
        <div v-if="configured.length || modelValue.length" class="flex flex-wrap gap-1.5">
            <span
                v-for="origin in configured"
                :key="`config-${origin}`"
                class="inline-flex max-w-full items-center gap-1.5 rounded-md border border-line bg-surface-subtle px-2 py-1 text-ink-muted"
                :title="$t('settings.frameAncestors.fromConfigHint')"
            >
                <UIcon name="i-heroicons-lock-closed" class="h-3.5 w-3.5 shrink-0" />
                <span class="truncate font-mono text-xs" dir="ltr">{{ origin }}</span>
                <span class="shrink-0 text-[11px]">· {{ $t('settings.frameAncestors.fromConfig') }}</span>
            </span>
            <span
                v-for="origin in modelValue"
                :key="origin"
                class="group inline-flex max-w-full items-center gap-1.5 rounded-md border border-line bg-surface-subtle py-1 ps-2 pe-1 transition-colors hover:border-line-strong"
                data-testid="frame-ancestors-item"
            >
                <UIcon name="i-heroicons-globe-alt" class="h-3.5 w-3.5 shrink-0 text-ink-subtle" />
                <span class="truncate font-mono text-xs" dir="ltr">{{ origin }}</span>
                <button
                    v-if="canEdit"
                    type="button"
                    class="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded text-ink-subtle opacity-60 transition hover:bg-black/5 hover:text-ink hover:opacity-100 focus-visible:opacity-100 focus-visible:outline focus-visible:outline-2 focus-visible:outline-blue-500 dark:hover:bg-white/10"
                    :aria-label="$t('settings.frameAncestors.removeSite', { site: origin })"
                    @click="remove(origin)"
                >
                    <UIcon name="i-heroicons-x-mark" class="h-3 w-3" />
                </button>
            </span>
        </div>
        <p v-else class="text-xs text-ink-muted">{{ $t('settings.frameAncestors.empty') }}</p>

        <div v-if="canEdit" class="max-w-md">
            <div class="flex items-center gap-2">
                <!-- A URL reads left to right in every locale; the icon and its padding follow. -->
                <div dir="ltr" class="min-w-0 flex-1">
                    <UInput
                        id="frame-ancestors-input"
                        v-model="draft"
                        size="sm"
                        icon="i-heroicons-link"
                        :placeholder="$t('settings.frameAncestors.placeholder')"
                        :color="draftError ? 'red' : 'white'"
                        data-testid="frame-ancestors-input"
                        @keydown.enter.prevent="add"
                    />
                </div>
                <UButton size="sm" color="gray" variant="ghost" icon="i-heroicons-plus" :disabled="!draft.trim()" data-testid="frame-ancestors-add" @click="add">
                    {{ $t('settings.frameAncestors.add') }}
                </UButton>
            </div>
            <p v-if="draftError" class="mt-1 text-xs text-red-600 dark:text-red-400">{{ draftError }}</p>
        </div>
        <p v-else class="text-xs text-ink-muted">{{ $t('settings.frameAncestors.readOnly') }}</p>

        <p class="flex items-start gap-1.5 text-xs text-ink-muted">
            <UIcon name="i-heroicons-information-circle" class="mt-px h-3.5 w-3.5 shrink-0" />
            <span>{{ $t('settings.frameAncestors.ssoNote') }}</span>
        </p>
    </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'

// The server checks every address on save; this only catches the common
// slips (no scheme, a path) before the admin presses Save.
const LOOKS_LIKE_ORIGIN = /^(https:\/\/(\*\.)?[a-z0-9.-]+|http:\/\/(localhost|127\.0\.0\.1))(:(\d{1,5}|\*))?\/?$/i

const props = defineProps<{ modelValue: string[]; configured: string[]; canEdit: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: string[]] }>()
const { t } = useI18n()

const draft = ref('')
const draftError = ref('')

function normalize(value: string): string {
    return value.trim().replace(/\/$/, '').toLowerCase()
}

function add() {
    const origin = normalize(draft.value)
    if (!origin) return
    if (!LOOKS_LIKE_ORIGIN.test(origin)) {
        draftError.value = t('settings.frameAncestors.invalidFormat')
        return
    }
    if (props.modelValue.includes(origin) || props.configured.includes(origin)) {
        draftError.value = t('settings.frameAncestors.duplicate')
        return
    }
    emit('update:modelValue', [...props.modelValue, origin])
    draft.value = ''
    draftError.value = ''
}

function remove(origin: string) {
    emit('update:modelValue', props.modelValue.filter((o) => o !== origin))
}

// Called by the page before saving, so an address typed but not added is
// not silently dropped. An address already listed just clears the field.
// False only when the address is not valid.
function commitDraft(): boolean {
    const origin = normalize(draft.value)
    if (!origin || props.modelValue.includes(origin) || props.configured.includes(origin)) {
        draft.value = ''
        draftError.value = ''
        return true
    }
    add()
    return !draftError.value
}

defineExpose({ commitDraft })
</script>
