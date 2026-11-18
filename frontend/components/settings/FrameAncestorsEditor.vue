<template>
    <div class="space-y-3" data-testid="frame-ancestors-editor">
        <div v-if="modelValue.length" class="flex flex-wrap gap-1.5">
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
                    @click="askRemove(origin)"
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
                        :color="draftError || serverError ? 'red' : 'white'"
                        data-testid="frame-ancestors-input"
                        @keydown.enter.prevent="add"
                        @blur="commitDraft"
                    />
                </div>
                <UButton size="sm" color="gray" variant="ghost" icon="i-heroicons-plus" :disabled="!draft.trim()" data-testid="frame-ancestors-add" @click="add">
                    {{ $t('settings.frameAncestors.add') }}
                </UButton>
            </div>
            <p v-if="draftError || serverError" class="mt-1 text-xs text-red-600 dark:text-red-400" role="alert" data-testid="frame-ancestors-error">{{ draftError || serverError }}</p>
        </div>
        <p v-else class="text-xs text-ink-muted">{{ $t('settings.frameAncestors.readOnly') }}</p>

        <div class="space-y-1 text-xs text-ink-muted">
            <p class="flex items-start gap-1.5">
                <UIcon name="i-heroicons-information-circle" class="mt-px h-3.5 w-3.5 shrink-0" />
                <span>{{ $t('settings.frameAncestors.ssoNote') }}</span>
            </p>
            <p class="flex items-start gap-1.5">
                <UIcon name="i-heroicons-eye" class="mt-px h-3.5 w-3.5 shrink-0" />
                <span>{{ $t('settings.frameAncestors.publicNote') }}</span>
            </p>
        </div>

        <!-- Removing a site stops BOW from showing inside it right away, so
             the list asks first. Adding a site needs no confirmation. -->
        <UModal v-model="confirmOpen" :ui="{ width: 'sm:max-w-md' }">
            <UCard :ui="{ body: { padding: 'p-6' }, header: { padding: 'px-6 py-4' }, footer: { padding: 'px-6 py-4' } }" data-testid="frame-ancestors-confirm">
                <template #header>
                    <h3 class="text-base font-semibold text-ink">{{ $t('settings.frameAncestors.confirmRemoveTitle') }}</h3>
                </template>
                <p class="text-sm text-ink-muted">
                    <i18n-t keypath="settings.frameAncestors.confirmRemoveBody" tag="span">
                        <template #site><span class="font-mono text-ink" dir="ltr">{{ pendingRemoval }}</span></template>
                    </i18n-t>
                </p>
                <template #footer>
                    <div class="flex justify-end gap-2">
                        <UButton color="gray" variant="ghost" @click="confirmOpen = false">{{ $t('common.cancel') }}</UButton>
                        <UButton color="red" data-testid="frame-ancestors-confirm-remove" @click="confirmRemove">{{ $t('common.remove') }}</UButton>
                    </div>
                </template>
            </UCard>
        </UModal>
    </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'

// The server checks every address on save; this only catches the common
// slips (no scheme, a path) before the list is saved.
const LOOKS_LIKE_ORIGIN = /^(https:\/\/(\*\.)?[a-z0-9.-]+|http:\/\/(localhost|127\.0\.0\.1))(:(\d{1,5}|\*))?\/?$/i

// serverError: the server's refusal of the last save, shown under the input.
const props = defineProps<{ modelValue: string[]; canEdit: boolean; serverError?: string }>()
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
    if (props.modelValue.includes(origin)) {
        draftError.value = t('settings.frameAncestors.duplicate')
        return
    }
    emit('update:modelValue', [...props.modelValue, origin])
    draft.value = ''
    draftError.value = ''
}

const confirmOpen = ref(false)
const pendingRemoval = ref('')

function askRemove(origin: string) {
    pendingRemoval.value = origin
    confirmOpen.value = true
}

function confirmRemove() {
    const origin = pendingRemoval.value
    confirmOpen.value = false
    emit('update:modelValue', props.modelValue.filter((o) => o !== origin))
}

// Runs when the input loses focus, so an address typed but not added is
// not silently dropped. An address already listed just clears the field.
// False only when the address is not valid.
function commitDraft(): boolean {
    const origin = normalize(draft.value)
    if (!origin || props.modelValue.includes(origin)) {
        draft.value = ''
        draftError.value = ''
        return true
    }
    add()
    return !draftError.value
}

defineExpose({ commitDraft })
</script>
