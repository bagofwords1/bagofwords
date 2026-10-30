<template>
  <!-- The sign-in popup of an artifact embedded in an SSO-mode host
       application (EmbeddedArtifactGate). BOW's own sign-in (SSO, password,
       LDAP) runs here, first-party; this page then hands the frame that
       opened it a one-time code bound to the frame's PKCE challenge. -->
  <main class="flex min-h-screen items-center justify-center bg-gray-50 px-5 dark:bg-gray-950">
    <div class="w-full max-w-sm rounded-2xl border border-gray-200 bg-white px-7 py-10 text-center shadow-sm dark:border-gray-800 dark:bg-gray-900"
         data-testid="embed-signin" :data-status="state">
      <img src="/assets/logo-128.png" alt="Bag of Words" class="mx-auto h-10 w-10" />
      <Spinner v-if="state === 'working'" class="mx-auto mt-5 h-6 w-6 text-blue-500" />
      <p class="mt-4 text-sm text-gray-700 dark:text-gray-200">{{ message }}</p>
    </div>
  </main>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'

definePageMeta({ layout: false, auth: false })

const route = useRoute()
const { t } = useI18n()
const { status, getSession } = useAuth()
const { getErrorMessage } = useErrorMessage()

const state = ref<'working' | 'done' | 'failed'>('working')
const message = ref(t('embed.signin.working'))

function stop(text: string) {
  state.value = 'failed'
  message.value = text
}

onMounted(async () => {
  const host = String(route.query.host || '')
  const challenge = String(route.query.challenge || '')
  const nonce = String(route.query.state || '')
  // Only the frame on this origin that opened us may receive the code.
  const opener = window.opener as Window | null
  if (!/^host_[a-f0-9]{8,32}$/.test(host) || challenge.length < 43 || !nonce || !opener) {
    stop(t('embed.signin.invalid'))
    return
  }
  await getSession()
  if (status.value !== 'authenticated') {
    await navigateTo(`/users/sign-in?redirect=${encodeURIComponent(window.location.pathname + window.location.search)}`)
    return
  }
  const { data, error } = await useMyFetch(`/api/embed/${host}/sso/code`, {
    method: 'POST',
    body: { code_challenge: challenge },
  })
  if (error.value || !(data.value as any)?.code) {
    stop(getErrorMessage(error.value))
    return
  }
  opener.postMessage({ type: 'bow:sso-code', code: (data.value as any).code, state: nonce }, window.location.origin)
  state.value = 'done'
  message.value = t('embed.signin.done')
  setTimeout(() => window.close(), 300)
})
</script>
