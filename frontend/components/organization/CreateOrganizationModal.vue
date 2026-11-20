<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-md' }" :prevent-close="isSubmitting">
    <form class="p-5" novalidate data-testid="create-organization-dialog" @submit.prevent="submit">
      <div class="flex items-start justify-between gap-3">
        <div class="min-w-0">
          <h3 class="text-base font-semibold text-ink">{{ $t('createOrganization.title') }}</h3>
          <p class="mt-1 text-sm text-ink-muted">{{ $t('createOrganization.description') }}</p>
        </div>
        <UButton
          color="gray"
          variant="ghost"
          size="xs"
          icon="i-heroicons-x-mark-20-solid"
          :aria-label="$t('common.close')"
          :disabled="isSubmitting"
          @click="close"
        />
      </div>

      <div class="mt-4 space-y-4">
        <UFormGroup :label="$t('createOrganization.nameLabel')" name="name" required :error="nameError || undefined">
          <UInput
            ref="nameInput"
            v-model="name"
            v-text-dir
            :maxlength="NAME_MAX_LENGTH"
            :placeholder="$t('createOrganization.namePlaceholder')"
            :disabled="isSubmitting"
            autocomplete="off"
            data-testid="create-organization-name"
            @update:model-value="clearErrors"
          />
        </UFormGroup>

        <UFormGroup
          :label="$t('createOrganization.adminEmailLabel')"
          :hint="$t('createOrganization.optional')"
          :help="$t('createOrganization.adminEmailHint')"
          name="first_admin_email"
          :error="emailError || undefined"
        >
          <UInput
            v-model="adminEmail"
            type="email"
            dir="ltr"
            :placeholder="$t('createOrganization.adminEmailPlaceholder')"
            :disabled="isSubmitting"
            autocomplete="off"
            data-testid="create-organization-admin-email"
            @update:model-value="clearErrors"
          />
        </UFormGroup>

        <p
          v-if="formError"
          role="alert"
          class="text-sm text-red-600 dark:text-red-400"
          data-testid="create-organization-error"
        >
          {{ formError }}
        </p>
      </div>

      <div class="mt-5 flex justify-end gap-2">
        <UButton
          color="gray"
          variant="soft"
          :disabled="isSubmitting"
          data-testid="create-organization-cancel"
          @click="close"
        >
          {{ $t('common.cancel') }}
        </UButton>
        <UButton
          type="submit"
          color="blue"
          :loading="isSubmitting"
          :disabled="!name.trim() || isSubmitting"
          data-testid="create-organization-submit"
        >
          {{ $t('createOrganization.submit') }}
        </UButton>
      </div>
    </form>
  </UModal>
</template>

<script setup lang="ts">
// Same cap as the organization name field in Settings → General.
const NAME_MAX_LENGTH = 80
// Deliberately loose: the server is the judge of what an address is; this only
// catches an obvious slip before the request is sent.
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
// Long enough to read the confirmation before the page reloads into the new
// organization; longer when the invite needs the creator's attention.
const SWITCH_DELAY_MS = 1200
const SWITCH_DELAY_WITH_NOTICE_MS = 4000

type CreatedOrganization = {
  id: string
  name: string
  first_admin_invite_status?: string | null
}

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void
  (e: 'created', organization: CreatedOrganization): void
}>()

const { t } = useI18n()
const toast = useToast()
const { getErrorMessage, getErrorCode } = useErrorMessage()
const { setOrganization } = useOrganization()

const isOpen = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const name = ref('')
const adminEmail = ref('')
const nameError = ref('')
const emailError = ref('')
const formError = ref('')
const isSubmitting = ref(false)
const nameInput = ref<any>(null)

const NAME_ERROR_CODES = new Set([
  'organization.name_required',
  'organization.name_too_long',
  'organization.name_taken',
])

function clearErrors() {
  nameError.value = ''
  emailError.value = ''
  formError.value = ''
}

function reset() {
  name.value = ''
  adminEmail.value = ''
  clearErrors()
}

function close() {
  if (isSubmitting.value) return
  isOpen.value = false
}

watch(isOpen, async (open) => {
  if (!open) return
  reset()
  await nextTick()
  nameInput.value?.input?.focus?.()
})

function isEmailFieldError(error: any): boolean {
  const detail = error?.data?.detail
  return Array.isArray(detail) && detail.some((item: any) => (item?.loc || []).includes('first_admin_email'))
}

function inviteNotice(status: string | null | undefined, email: string): { text: string, warn: boolean } | null {
  switch (status) {
    case 'sent': return { text: t('createOrganization.inviteSent', { email }), warn: false }
    case 'added': return { text: t('createOrganization.adminAdded', { email }), warn: false }
    case 'skipped_no_smtp': return { text: t('createOrganization.inviteNoSmtp', { email }), warn: true }
    case 'failed':
    case 'not_invited': return { text: t('createOrganization.inviteNotSent', { email }), warn: true }
    default: return null
  }
}

async function submit() {
  if (isSubmitting.value) return
  clearErrors()

  const trimmedName = name.value.trim()
  const trimmedEmail = adminEmail.value.trim()
  if (!trimmedName) {
    nameError.value = t('createOrganization.nameRequired')
    return
  }
  if (trimmedEmail && !EMAIL_PATTERN.test(trimmedEmail)) {
    emailError.value = t('createOrganization.adminEmailInvalid')
    return
  }

  isSubmitting.value = true
  try {
    const { data, error } = await useMyFetch<CreatedOrganization>('/organizations', {
      method: 'POST',
      body: { name: trimmedName, first_admin_email: trimmedEmail || null },
    })
    if (error.value) throw error.value
    const created = data.value as CreatedOrganization | null
    if (!created?.id) throw new Error('No organization in response')

    const notice = inviteNotice(created.first_admin_invite_status, trimmedEmail)
    toast.add({
      title: t('createOrganization.created', { name: created.name }),
      description: [notice?.text, t('createOrganization.switching')].filter(Boolean).join(' '),
      icon: notice?.warn ? 'i-heroicons-exclamation-triangle' : 'i-heroicons-check-circle',
      color: notice?.warn ? 'orange' : 'green',
      timeout: notice?.warn ? SWITCH_DELAY_WITH_NOTICE_MS : SWITCH_DELAY_MS,
    })
    emit('created', created)
    isOpen.value = false
    // setOrganization reloads the app, which also refreshes the session's
    // organization list.
    setTimeout(() => setOrganization(created.id), notice?.warn ? SWITCH_DELAY_WITH_NOTICE_MS : SWITCH_DELAY_MS)
  } catch (error: any) {
    const message = getErrorMessage(error, t('createOrganization.failed'))
    if (NAME_ERROR_CODES.has(getErrorCode(error) || '')) nameError.value = message
    else if (isEmailFieldError(error)) emailError.value = t('createOrganization.adminEmailInvalid')
    else formError.value = message
  } finally {
    isSubmitting.value = false
  }
}
</script>
