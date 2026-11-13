<template>
  <div class="min-h-screen bg-gray-50 dark:bg-gray-900 flex items-center justify-center py-12 px-4">
    <div class="w-full max-w-6xl relative">
      <Transition name="fade" mode="out-in">
        <div v-if="loading" key="loading" class="flex items-center justify-center h-40">
          <div class="flex items-center text-gray-500 dark:text-gray-400 text-sm">
            <Spinner class="w-4 h-4 me-2" />
            {{ $t('onboarding.loading') }}
          </div>
        </div>
        <div v-else key="content">
          <OnboardingView forcedStepKey="onboarding" :hideNextButton="false" :hideSidebar="true" :beforeNext="saveProductUpdates">
            <template #onboarding>
              <div>
                <div class="mb-5">
                    <p class="text-sm text-gray-600 dark:text-gray-400 mb-5">
                      {{ $t('onboarding.intro1') }}
                    </p>
                  <p class="text-sm text-gray-600 dark:text-gray-400">
                    {{ $t('onboarding.intro2') }}
                  </p>
                </div>
                <!-- The first admin who signed up through SSO never saw the
                     sign-up checkbox: ask once here. "Skip" sends nothing. -->
                <ProductUpdatesOptIn
                  v-if="productUpdatesAsk"
                  v-model="productUpdates"
                  :email="productUpdatesEmail"
                  input-id="onboarding-product-updates"
                  class="mt-2"
                />
              </div>

            </template>

          </OnboardingView>
          <div class="text-center">
            <button @click="skipForNow" class="text-gray-500 dark:text-gray-400 hover:text-gray-700 text-sm inline text-sm mt-4 rounded-md px-3 py-1.5">{{ $t('onboarding.skip') }}</button>
          </div>
        </div>
      </Transition>
    </div>
  </div>
</template>

<script setup lang="ts">
definePageMeta({ auth: true, layout: 'users' })
import OnboardingView from '@/components/onboarding/OnboardingView.vue'
import Spinner from '@/components/Spinner.vue'
import ProductUpdatesOptIn from '@/components/ProductUpdatesOptIn.vue'
const { updateOnboarding } = useOnboarding()
const router = useRouter()
const { fetchOnboarding, onboarding } = useOnboarding()
const loading = ref(true)
const productUpdatesAsk = ref(false)
const productUpdates = ref(false)
const productUpdatesEmail = ref<string | null>(null)

async function loadProductUpdates() {
  try {
    // 403 for non-admins; unavailable when telemetry is off or on the cloud.
    const resp = await useMyFetch('/api/product-updates')
    const s = resp.data.value as { available?: boolean; decided?: boolean; current_user_email?: string } | null
    if (resp.status.value === 'success' && s?.available && !s.decided) {
      productUpdatesAsk.value = true
      productUpdatesEmail.value = s.current_user_email ?? null
    }
  } catch (_) {}
}

async function saveProductUpdates() {
  if (!productUpdatesAsk.value) return
  await useMyFetch('/api/product-updates', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ opted_in: productUpdates.value, source: 'onboarding' }),
  })
}

onMounted(async () => {
  try {
    await Promise.all([fetchOnboarding(), loadProductUpdates()])
  } finally {
    loading.value = false
  }
})
async function skipForNow() { await updateOnboarding({ dismissed: true }); router.push('/') }
</script>


