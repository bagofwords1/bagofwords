<template>
  <div>
    <div class="settings-disclosure-group">
      <!-- Entra ID profile sync -->
      <ProfileSyncSection
        i18n-prefix="entra"
        endpoint="/api/organization/identity/entra-profile-sync"
        :allowed-fields="ENTRA_PROFILE_FIELDS"
        :default-visible="ENTRA_DEFAULT_VISIBLE"
      />

      <!-- Google profile sync -->
      <ProfileSyncSection
        i18n-prefix="google"
        endpoint="/api/organization/identity/google-profile-sync"
        :allowed-fields="GOOGLE_PROFILE_FIELDS"
        :default-visible="GOOGLE_DEFAULT_VISIBLE"
      />

      <!-- SCIM provisioning -->
      <SettingsDisclosure v-model:open="scimOpen" :title="$t('settings.identityProvider.scimTitle')" :description="$t('settings.identityProvider.scimSubtitle')">
        <template v-if="!hasFeature('scim')" #badge>
          <UBadge color="gray" variant="subtle" size="xs">{{ $t('settings.identityProvider.notLicensed') }}</UBadge>
        </template>

        <!-- Enterprise gate -->
        <div v-if="!hasFeature('scim')" class="rounded-lg bg-surface-subtle px-4 py-3">
          <p class="text-sm text-ink-2">{{ $t('settings.identityProvider.enterpriseScim') }}</p>
          <UButton to="https://docs.bagofwords.com/enterprise" target="_blank" color="gray" variant="link" size="sm" trailing-icon="i-heroicons-arrow-up-right" class="mt-1 -ms-2.5 px-0">
            {{ $t('settings.identityProvider.learnMore') }}
          </UButton>
        </div>

        <template v-else>
          <!-- SCIM endpoint URL -->
          <SettingsRow :label="$t('settings.identityProvider.scimBaseUrl')" :description="$t('settings.identityProvider.scimBaseUrlHint')" stacked>
            <div class="flex items-center gap-2">
              <code class="flex-1 min-w-0 truncate font-mono text-xs bg-surface-subtle text-ink-2 px-3 py-2 rounded-md border border-line">{{ scimBaseUrl }}</code>
              <UButton color="gray" size="sm" :icon="copied === 'url' ? 'i-heroicons-check' : 'i-heroicons-clipboard-document'" @click="copyToClipboard(scimBaseUrl)">
                {{ copied === 'url' ? $t('settings.identityProvider.copied') : $t('settings.identityProvider.copy') }}
              </UButton>
            </div>
          </SettingsRow>

          <!-- Tokens -->
          <div class="mt-6 flex items-center justify-between">
            <span class="text-sm font-medium text-ink">{{ $t('settings.identityProvider.bearerTokens') }}</span>
            <UButton size="sm" color="gray" icon="i-heroicons-plus" @click="showCreateModal = true">
              {{ $t('settings.identityProvider.generateToken') }}
            </UButton>
          </div>

          <div class="mt-3">
            <div v-if="scimLoading" class="py-8 flex justify-center"><Spinner class="w-4 h-4 text-ink-muted" /></div>
            <div v-else-if="scimError" class="py-6 text-center text-sm text-red-600 dark:text-red-400">{{ scimError }}</div>
            <div v-else class="bow-table-card">
              <table v-if="tokens.length > 0" class="bow-table">
                <tbody>
                  <tr v-for="token in tokens" :key="token.id">
                    <td><span class="bow-cell-primary">{{ token.name }}</span></td>
                    <td><span class="font-mono text-xs text-ink-muted">{{ token.token_prefix }}…</span></td>
                    <td class="text-xs text-ink-muted">
                      <template v-if="token.last_used_at">{{ $t('settings.identityProvider.lastUsed', { when: formatRelativeTime(token.last_used_at) }) }}</template>
                      <template v-else>{{ $t('settings.identityProvider.neverUsed') }}</template>
                    </td>
                    <td class="text-xs text-ink-muted whitespace-nowrap">{{ formatRelativeTime(token.created_at) }}</td>
                    <td class="text-end">
                      <UButton color="red" variant="ghost" size="xs" class="bow-row-actions" @click="confirmRevoke(token)">
                        {{ $t('settings.identityProvider.revoke') }}
                      </UButton>
                    </td>
                  </tr>
                </tbody>
              </table>
              <SettingsEmpty v-else icon="i-heroicons-key" :title="$t('settings.identityProvider.noTokens')" :description="$t('settings.identityProvider.noTokensHint')" compact />
            </div>
          </div>
        </template>
      </SettingsDisclosure>

      <!-- LDAP directory sync -->
      <SettingsDisclosure v-model:open="ldapOpen" :title="$t('settings.identityProvider.ldapTitle')" :description="$t('settings.identityProvider.ldapSubtitle')">
        <template v-if="!hasFeature('ldap')" #badge>
          <UBadge color="gray" variant="subtle" size="xs">{{ $t('settings.identityProvider.notLicensed') }}</UBadge>
        </template>

        <div v-if="!hasFeature('ldap')" class="rounded-lg bg-surface-subtle px-4 py-3">
          <p class="text-sm text-ink-2">{{ $t('settings.identityProvider.enterpriseLdap') }}</p>
          <UButton to="https://docs.bagofwords.com/enterprise" target="_blank" color="gray" variant="link" size="sm" trailing-icon="i-heroicons-arrow-up-right" class="mt-1 -ms-2.5 px-0">
            {{ $t('settings.identityProvider.learnMore') }}
          </UButton>
        </div>

        <template v-else>
          <!-- Not configured -->
          <div v-if="!ldapStatus?.ldap_configured" class="rounded-lg border border-dashed border-line">
            <SettingsEmpty icon="i-heroicons-server-stack" :title="$t('settings.identityProvider.ldapNotConfigured')" :description="$t('settings.identityProvider.ldapNotConfiguredHint')" compact />
          </div>

          <template v-else>
            <!-- Connection status -->
            <div class="rounded-lg border border-line px-4 py-3 flex items-center justify-between gap-4">
              <div class="min-w-0">
                <div class="flex items-center gap-2">
                  <span class="w-2 h-2 rounded-full shrink-0" :class="ldapTestResult?.connected ? 'bg-green-500' : (ldapTestResult ? 'bg-red-500' : 'bg-gray-300 dark:bg-gray-600')" />
                  <span class="text-sm font-medium text-ink">
                    {{ ldapTestResult?.connected ? $t('settings.identityProvider.statusConnected') : (ldapTestResult ? $t('settings.identityProvider.statusFailed') : $t('settings.identityProvider.statusNotTested')) }}
                  </span>
                </div>
                <p v-if="ldapTestResult?.connected" class="text-xs text-ink-muted mt-0.5 ms-4 truncate">
                  {{ ldapTestResult.server }}
                  <template v-if="ldapTestResult.vendor"> · {{ ldapTestResult.vendor }}</template>
                  <template v-if="ldapTestResult.user_count !== null"> · {{ ldapTestResult.user_count }} users</template>
                  <template v-if="ldapTestResult.group_count !== null"> · {{ ldapTestResult.group_count }} groups</template>
                </p>
                <p v-if="ldapTestResult && !ldapTestResult.connected" class="text-xs text-red-600 dark:text-red-400 mt-0.5 ms-4">{{ ldapTestResult.error }}</p>
              </div>
              <UButton color="gray" size="sm" :loading="ldapLoading" :disabled="ldapLoading" @click="handleTestConnection">
                {{ ldapLoading ? $t('settings.identityProvider.testing') : $t('settings.identityProvider.testConnection') }}
              </UButton>
            </div>

            <!-- Last sync -->
            <div v-if="ldapStatus?.last_sync" class="mt-3 rounded-lg border border-line px-4 py-3">
              <span class="text-sm font-medium text-ink">{{ $t('settings.identityProvider.lastSync') }}</span>
              <p class="text-xs text-ink-muted mt-0.5">
                {{ ldapStatus.last_sync.timestamp ? formatRelativeTime(ldapStatus.last_sync.timestamp) : $t('settings.identityProvider.unknown') }}
                {{ $t('settings.identityProvider.lastSyncDetail', {
                  gCreated: ldapStatus.last_sync.groups_created,
                  gUpdated: ldapStatus.last_sync.groups_updated,
                  gRemoved: ldapStatus.last_sync.groups_removed,
                  mAdded: ldapStatus.last_sync.memberships_added,
                  mRemoved: ldapStatus.last_sync.memberships_removed,
                }) }}
              </p>
              <p v-if="ldapStatus.last_sync.errors.length" class="text-xs text-red-600 dark:text-red-400 mt-0.5">
                {{ $t('settings.identityProvider.errorCount', { n: ldapStatus.last_sync.errors.length, first: ldapStatus.last_sync.errors[0] }) }}
              </p>
            </div>

            <!-- Sync actions -->
            <div class="mt-4 flex items-center gap-2">
              <UButton color="blue" :loading="ldapLoading" :disabled="ldapLoading" @click="handleSync">
                {{ ldapLoading ? $t('settings.identityProvider.syncing') : $t('settings.identityProvider.syncNow') }}
              </UButton>
              <UButton color="gray" :disabled="ldapLoading" @click="handlePreview">
                {{ $t('settings.identityProvider.previewChanges') }}
              </UButton>
            </div>

            <!-- Sync result -->
            <div v-if="lastSyncResult" class="mt-3 rounded-lg border border-green-200 dark:border-green-900/60 bg-green-50 dark:bg-green-950/40 px-4 py-3 text-sm text-green-800 dark:text-green-200">
              {{ $t('settings.identityProvider.syncCompletedSummary', {
                gCreated: lastSyncResult.groups_created,
                gUpdated: lastSyncResult.groups_updated,
                gRemoved: lastSyncResult.groups_removed,
                mAdded: lastSyncResult.memberships_added,
                mRemoved: lastSyncResult.memberships_removed,
              }) }}
              <template v-if="lastSyncResult.users_not_found">
                {{ $t('settings.identityProvider.ldapUsersNotFound', { n: lastSyncResult.users_not_found }) }}
              </template>
            </div>

            <!-- Preview -->
            <div v-if="ldapPreview" class="mt-3 bow-table-card">
              <div class="px-5 py-3 border-b border-line text-sm">
                <span class="font-medium text-ink">{{ $t('settings.identityProvider.preview') }}</span>
                <span class="text-ink-muted">
                  {{ $t('settings.identityProvider.previewSummary', {
                    create: ldapPreview.groups_to_create,
                    update: ldapPreview.groups_to_update,
                    remove: ldapPreview.groups_to_remove,
                    changes: ldapPreview.total_membership_changes,
                  }) }}
                </span>
              </div>
              <div v-if="ldapPreview.groups.length" class="max-h-64 overflow-y-auto">
                <table class="bow-table">
                  <tbody>
                    <tr v-for="group in ldapPreview.groups" :key="group.dn">
                      <td class="truncate" :title="group.dn"><span class="bow-cell-primary">{{ group.name }}</span></td>
                      <td class="text-xs text-ink-muted whitespace-nowrap">{{ $t('settings.identityProvider.memberCount', { n: group.member_count }) }}</td>
                      <td class="text-xs whitespace-nowrap" :class="group.exists_in_app ? 'text-ink-muted' : 'text-blue-600 dark:text-blue-400'">
                        {{ group.exists_in_app ? $t('settings.identityProvider.groupExists') : $t('settings.identityProvider.groupNew') }}
                      </td>
                      <td class="text-end text-xs tabular-nums whitespace-nowrap">
                        <span v-if="group.members_to_add" class="text-green-700 dark:text-green-400 me-2">+{{ group.members_to_add }}</span>
                        <span v-if="group.members_to_remove" class="text-red-600 dark:text-red-400">−{{ group.members_to_remove }}</span>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div v-else class="py-6 text-center text-sm text-ink-muted">{{ $t('settings.identityProvider.noLdapGroups') }}</div>
            </div>

            <p v-if="ldapError" class="mt-3 text-sm text-red-600 dark:text-red-400">{{ ldapError }}</p>
          </template>
        </template>
      </SettingsDisclosure>
    </div>

    <!-- ================================================================== -->
    <!-- SCIM Modals (Create + Revoke)                                      -->
    <!-- ================================================================== -->

    <!-- Create token -->
    <UModal :model-value="showCreateModal" :ui="{ width: 'sm:max-w-md' }" @update:model-value="v => { if (!v) dismissCreateModal() }">
      <div class="p-6">
        <h3 class="text-base font-semibold text-ink">{{ $t('settings.identityProvider.generateTokenTitle') }}</h3>

        <template v-if="!createdToken">
          <div class="mt-4">
            <SettingsRow :label="$t('settings.identityProvider.nameLabel')" stacked for-id="scim-token-name">
              <UInput id="scim-token-name" v-model="newTokenName" autofocus :placeholder="$t('settings.identityProvider.namePlaceholder')" @keydown.enter="handleCreateToken" />
            </SettingsRow>
          </div>
          <div class="mt-6 flex justify-end gap-2">
            <UButton color="gray" variant="ghost" @click="dismissCreateModal">{{ $t('settings.identityProvider.cancel') }}</UButton>
            <UButton color="blue" :loading="creating" :disabled="!newTokenName.trim() || creating" @click="handleCreateToken">
              {{ creating ? $t('settings.identityProvider.generating') : $t('settings.identityProvider.generate') }}
            </UButton>
          </div>
        </template>

        <template v-else>
          <div class="mt-4 rounded-lg border border-amber-200 dark:border-amber-900/60 bg-amber-50 dark:bg-amber-950/40 px-4 py-3 flex items-start gap-2">
            <UIcon name="i-heroicons-exclamation-triangle" class="w-4 h-4 text-amber-600 dark:text-amber-400 mt-0.5 shrink-0" />
            <p class="text-sm font-medium text-amber-800 dark:text-amber-200">{{ $t('settings.identityProvider.copyWarning') }}</p>
          </div>
          <div class="mt-3 flex items-center gap-2">
            <code class="flex-1 min-w-0 truncate font-mono text-xs bg-surface-subtle text-ink-2 px-3 py-2 rounded-md border border-line">{{ createdToken }}</code>
            <UButton color="gray" size="sm" :icon="copied === 'token' ? 'i-heroicons-check' : 'i-heroicons-clipboard-document'" @click="copyToClipboard(createdToken!, 'token')">
              {{ copied === 'token' ? $t('settings.identityProvider.copied') : $t('settings.identityProvider.copy') }}
            </UButton>
          </div>
          <div class="mt-6 flex justify-end">
            <UButton color="blue" @click="dismissCreateModal">{{ $t('settings.identityProvider.done') }}</UButton>
          </div>
        </template>
      </div>
    </UModal>

    <!-- Revoke confirmation -->
    <UModal :model-value="!!tokenToRevoke" :ui="{ width: 'sm:max-w-md' }" @update:model-value="v => { if (!v) tokenToRevoke = null }">
      <div v-if="tokenToRevoke" class="p-6">
        <h3 class="text-base font-semibold text-ink">{{ $t('settings.identityProvider.revokeTitle') }}</h3>
        <p class="mt-2 text-sm text-ink-muted leading-relaxed">
          {{ $t('settings.identityProvider.revokeWarning', { name: tokenToRevoke.name }) }}
        </p>
        <div class="mt-6 flex justify-end gap-2">
          <UButton color="gray" variant="ghost" @click="tokenToRevoke = null">{{ $t('settings.identityProvider.cancel') }}</UButton>
          <UButton color="red" @click="handleRevoke">{{ $t('settings.identityProvider.revoke') }}</UButton>
        </div>
      </div>
    </UModal>
  </div>
</template>

<script setup lang="ts">
import { useScimTokens, type ScimToken } from '~/ee/composables/useScimTokens'
import { useLdapSync, type SyncResult as LDAPSyncResult } from '~/ee/composables/useLdapSync'
import { ENTRA_PROFILE_FIELDS, GOOGLE_PROFILE_FIELDS } from '~/composables/useProfileSync'
import SettingsDisclosure from '~/components/settings/SettingsDisclosure.vue'
import SettingsRow from '~/components/settings/SettingsRow.vue'
import SettingsEmpty from '~/components/settings/SettingsEmpty.vue'
import Spinner from '~/components/Spinner.vue'

definePageMeta({
  auth: true,
  permissions: ['manage_identity_providers']
})

const { t } = useI18n()
const { hasFeature, license } = useEnterprise()

// ── Collapsible section state (all collapsed by default) ──
const scimOpen = ref(false)
const ldapOpen = ref(false)

// ── Profile sync sections (Entra + Google) ──
// Fields shown as default rows when nothing is configured yet.
const ENTRA_DEFAULT_VISIBLE = ['jobTitle', 'department', 'companyName', 'officeLocation']
const GOOGLE_DEFAULT_VISIBLE = ['displayName', 'jobTitle', 'department', 'organization']

// ── SCIM ──
const { tokens, loading: scimLoading, error: scimError, fetchTokens, createToken, revokeToken } = useScimTokens()

const showCreateModal = ref(false)
const newTokenName = ref('SCIM Token')
const creating = ref(false)
const createdToken = ref<string | null>(null)
const tokenToRevoke = ref<ScimToken | null>(null)
const copied = ref<string | null>(null)
const hasFetchedScim = ref(false)

const scimBaseUrl = computed(() => {
  if (process.client) {
    return `${window.location.origin}/scim/v2`
  }
  return '/scim/v2'
})

const dismissCreateModal = () => {
  showCreateModal.value = false
  createdToken.value = null
  newTokenName.value = 'SCIM Token'
}

const handleCreateToken = async () => {
  if (!newTokenName.value.trim() || creating.value) return
  creating.value = true
  const result = await createToken(newTokenName.value.trim())
  creating.value = false
  if (result) {
    createdToken.value = result.token
  }
}

const confirmRevoke = (token: ScimToken) => {
  tokenToRevoke.value = token
}

const handleRevoke = async () => {
  if (!tokenToRevoke.value) return
  await revokeToken(tokenToRevoke.value.id)
  tokenToRevoke.value = null
  createdToken.value = null
}

// ── LDAP ──
const {
  status: ldapStatus,
  preview: ldapPreview,
  testResult: ldapTestResult,
  loading: ldapLoading,
  error: ldapError,
  fetchStatus: fetchLdapStatus,
  triggerSync,
  fetchPreview,
  testConnection,
} = useLdapSync()

const lastSyncResult = ref<LDAPSyncResult | null>(null)
const hasFetchedLdap = ref(false)

const handleTestConnection = async () => {
  await testConnection()
}

const handleSync = async () => {
  lastSyncResult.value = null
  ldapPreview.value = null
  const result = await triggerSync()
  if (result) {
    lastSyncResult.value = result
    setTimeout(() => { lastSyncResult.value = null }, 15000)
  }
}

const handlePreview = async () => {
  lastSyncResult.value = null
  await fetchPreview()
}

// ── Shared ──
const copyToClipboard = async (text: string, key: string = 'url') => {
  try {
    await navigator.clipboard.writeText(text)
    copied.value = key
    setTimeout(() => { copied.value = null }, 2000)
  } catch {
    // Fallback
  }
}

const { relativeTime: formatRelativeTime } = useRelativeTime()

// ── Init ──
watch(
  () => license.value,
  (newLicense) => {
    if (newLicense && hasFeature('scim') && !hasFetchedScim.value) {
      hasFetchedScim.value = true
      fetchTokens()
    }
    if (newLicense && hasFeature('ldap') && !hasFetchedLdap.value) {
      hasFetchedLdap.value = true
      fetchLdapStatus()
    }
  },
  { immediate: true }
)
</script>
