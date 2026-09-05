<template>
  <div class="space-y-2" v-if="googleEnabled || providers.length">
    <button
      v-if="googleEnabled"
      @click="start('google')"
      type="button"
      :disabled="loadingProvider !== null"
      :class="buttonClass"
    >
      <template v-if="loadingProvider === 'google'">
        <Spinner class="h-4 w-4 me-2" />
        {{ $t('auth.redirecting') }}
      </template>
      <template v-else>
        <img src="/icons/google.svg" alt="" aria-hidden="true" class="h-[18px] w-[18px] me-2.5" />
        {{ mode === 'sign-up' ? $t('auth.signUpWithGoogle') : $t('auth.signInWithGoogle') }}
      </template>
    </button>

    <button
      v-for="p in providers"
      :key="p.name"
      @click="start(p.name)"
      type="button"
      :disabled="loadingProvider !== null"
      :class="buttonClass"
    >
      <template v-if="loadingProvider === p.name">
        <Spinner class="h-4 w-4 me-2" />
        {{ $t('auth.redirecting') }}
      </template>
      <template v-else>
        <img v-if="iconSrc(p)" :src="iconSrc(p)" alt="" aria-hidden="true" class="h-[18px] w-[18px] me-2.5" />
        <UIcon v-else name="i-heroicons-key" aria-hidden="true" class="h-[18px] w-[18px] me-2.5 text-gray-400 dark:text-gray-500" />
        {{ mode === 'sign-up'
          ? $t('auth.continueWithProvider', { provider: label(p) })
          : $t('auth.signInWithProvider', { provider: label(p) }) }}
      </template>
    </button>
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'

interface AuthProvider {
  name: string
  enabled?: boolean
  // Derived server-side in /api/settings so the raw issuer (which can carry a
  // tenant id) never reaches an unauthenticated page.
  label?: string
  brand?: 'microsoft' | 'google' | 'custom'
  icon?: string | null
}

const props = withDefaults(defineProps<{
  providers: AuthProvider[]
  googleEnabled: boolean
  mode?: 'sign-in' | 'sign-up'
}>(), {
  mode: 'sign-in',
})

const emit = defineEmits<{ (e: 'error', message: string): void }>()

const { t } = useI18n()
const route = useRoute()
const loadingProvider = ref<string | null>(null)

const buttonClass =
  'w-full h-10 inline-flex items-center justify-center rounded-lg border border-gray-300 ' +
  'dark:border-gray-700 bg-white dark:bg-gray-900 text-sm font-medium text-gray-700 ' +
  'dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800 focus:outline-none ' +
  'focus:ring-2 focus:ring-blue-500/40 disabled:opacity-50 disabled:cursor-not-allowed transition-colors'

// Only three marks are shipped: the two providers most deployments use, and a
// neutral key for every other OIDC issuer (Okta, Keycloak, an in-house IdP).
const BRAND_ICONS: Record<string, string> = {
  google: '/icons/google.svg',
  microsoft: '/icons/microsoft.svg',
}

function iconSrc(p: AuthProvider): string | null {
  return p.icon || BRAND_ICONS[p.brand ?? ''] || null
}

// `name` is a routing slug, so it is the last resort — not the first choice.
function label(p: AuthProvider): string {
  return p.label || p.name
}

const OAUTH_REDIRECT_STORAGE_NAME = 'bow:postSignInRedirect'

// Only honor redirects to same-origin paths to avoid open-redirect bugs
function safeRedirectTarget(value: unknown): string | null {
  if (typeof value !== 'string' || !value) return null
  if (!value.startsWith('/') || value.startsWith('//')) return null
  return value
}

// The IdP round-trip loses the query string, so stash the post-login target.
function persistRedirectForOAuth() {
  const target = safeRedirectTarget(route.query.redirect)
  try {
    if (target) {
      sessionStorage.setItem(OAUTH_REDIRECT_STORAGE_NAME, target)
    } else {
      sessionStorage.removeItem(OAUTH_REDIRECT_STORAGE_NAME)
    }
  } catch (_) {}
}

async function start(name: string) {
  try {
    loadingProvider.value = name
    persistRedirectForOAuth()
    const response = await $fetch(`/api/auth/${name}/authorize`, { method: 'GET' })
    if ((response as any)?.authorization_url) {
      window.location.href = (response as any).authorization_url
      return
    }
    throw new Error('missing authorization_url')
  } catch (error) {
    loadingProvider.value = null
    const provider = props.providers.find((p) => p.name === name)
    emit('error', name === 'google'
      ? t('auth.googleInitError')
      : t('auth.providerInitError', { provider: provider ? label(provider) : name }))
  }
}
</script>
