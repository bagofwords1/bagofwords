<template>
  <div class="mt-4 space-y-4">

    <!-- ================================================================== -->
    <!-- Entra ID Profile Sync Section                                       -->
    <!-- ================================================================== -->
    <div class="border border-gray-200 dark:border-gray-700 rounded-lg">
      <button
        class="w-full flex items-center justify-between px-4 py-3 text-left"
        @click="entraOpen = !entraOpen"
      >
        <div>
          <h2 class="text-sm font-medium text-gray-900 dark:text-white">{{ $t('settings.identityProvider.entraTitle') }}</h2>
          <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('settings.identityProvider.entraSubtitle') }}</p>
        </div>
        <svg class="w-4 h-4 text-gray-400 transition-transform flex-shrink-0" :class="entraOpen ? 'rotate-180' : ''" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" d="m19.5 8.25-7.5 7.5-7.5-7.5" />
        </svg>
      </button>

      <div v-show="entraOpen" class="px-4 pb-4 border-t border-gray-100 dark:border-gray-800 pt-4">
        <!-- Enable toggle -->
        <div class="flex items-start justify-between gap-4 mb-4">
          <div class="flex-1">
            <div class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('settings.identityProvider.entraEnable') }}</div>
            <p class="text-[11px] text-gray-400 dark:text-gray-400 mt-0.5">{{ $t('settings.identityProvider.entraEnableHint') }}</p>
          </div>
          <button
            type="button"
            role="switch"
            :aria-checked="entraEnabled"
            class="relative inline-flex h-5 w-9 flex-shrink-0 rounded-full transition-colors focus:outline-none"
            :class="entraEnabled ? 'bg-blue-600' : 'bg-gray-200 dark:bg-gray-700'"
            @click="entraEnabled = !entraEnabled"
          >
            <span
              class="inline-block h-4 w-4 transform rounded-full bg-white transition-transform mt-0.5"
              :class="entraEnabled ? 'translate-x-4' : 'translate-x-0.5'"
            ></span>
          </button>
        </div>

        <!-- Fields to include in context -->
        <div v-show="entraEnabled">
          <div class="flex items-center justify-between mb-2">
            <label class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('settings.identityProvider.entraFieldsLabel') }}</label>
            <span v-if="previewLoading" class="text-[11px] text-gray-400">
              <span class="inline-block w-3 h-3 border-2 border-gray-200 dark:border-gray-700 border-t-gray-500 rounded-full animate-spin align-middle"></span>
            </span>
          </div>

          <!-- Preview connection hint -->
          <p v-if="preview && !preview.connected" class="text-[11px] text-amber-600 dark:text-amber-500 mb-2">
            {{ preview.error === 'no_entra_login'
              ? $t('settings.identityProvider.entraNoLogin')
              : $t('settings.identityProvider.entraPreviewError') }}
          </p>

          <!-- Field rows: checkbox (in context) | label | sample value | remove -->
          <div class="border border-gray-200 dark:border-gray-700 rounded overflow-hidden">
            <div
              v-for="(field, idx) in shownFields"
              :key="field"
              class="flex items-center gap-2 px-3 py-2 text-xs"
              :class="{ 'border-t border-gray-100 dark:border-gray-800': idx > 0 }"
            >
              <input
                :id="`entra-field-${field}`"
                type="checkbox"
                :checked="selected.has(field)"
                class="rounded border-gray-300 text-blue-600 focus:ring-blue-500"
                @change="toggleField(field)"
              />
              <label :for="`entra-field-${field}`" class="w-40 flex-shrink-0 text-gray-700 dark:text-gray-300 cursor-pointer">
                {{ fieldLabel(field) }}
              </label>
              <span class="flex-1 truncate font-mono text-[11px]" :class="sampleValue(field) ? 'text-gray-500 dark:text-gray-400' : 'text-gray-300 dark:text-gray-600 italic'">
                {{ sampleValue(field) || $t('settings.identityProvider.entraNotSet') }}
              </span>
              <button
                class="text-gray-300 hover:text-red-500 flex-shrink-0"
                :title="$t('settings.identityProvider.entraRemove')"
                @click="removeField(field)"
              >
                <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18 18 6M6 6l12 12" /></svg>
              </button>
            </div>
            <div v-if="!shownFields.length" class="px-3 py-4 text-center text-[11px] text-gray-400">
              {{ $t('settings.identityProvider.entraNoFields') }}
            </div>
          </div>

          <!-- Add attribute -->
          <div v-if="addableFields.length" class="flex items-center gap-2 mt-2">
            <select
              v-model="fieldToAdd"
              class="text-xs border border-gray-200 dark:border-gray-700 dark:bg-gray-900 rounded px-2 py-1.5 focus:outline-none focus:border-gray-400"
            >
              <option value="">{{ $t('settings.identityProvider.entraAddPlaceholder') }}</option>
              <option v-for="f in addableFields" :key="f" :value="f">{{ fieldLabel(f) }}</option>
            </select>
            <button
              class="px-2 py-1.5 text-xs text-gray-600 dark:text-gray-300 border border-gray-200 dark:border-gray-700 rounded hover:border-gray-300 disabled:opacity-40"
              :disabled="!fieldToAdd"
              @click="addField"
            >
              {{ $t('settings.identityProvider.entraAdd') }}
            </button>
          </div>
        </div>

        <!-- Save -->
        <div class="flex items-center gap-3 mt-4">
          <button
            class="px-3 py-1.5 text-xs text-white bg-blue-600 rounded hover:bg-blue-700 disabled:opacity-50"
            :disabled="entraSaving"
            @click="handleEntraSave"
          >
            {{ entraSaving ? $t('settings.identityProvider.entraSaving') : $t('common.save') }}
          </button>
          <span v-if="entraSavedFlash" class="text-[11px] text-green-600">{{ $t('settings.identityProvider.entraSaved') }}</span>
          <span v-if="entraError" class="text-[11px] text-red-500">{{ entraError }}</span>
        </div>
      </div>
    </div>

    <!-- ================================================================== -->
    <!-- SCIM Provisioning Section (collapsible)                             -->
    <!-- ================================================================== -->
    <div class="border border-gray-200 dark:border-gray-700 rounded-lg">
      <button
        class="w-full flex items-center justify-between px-4 py-3 text-left"
        @click="scimOpen = !scimOpen"
      >
        <div>
          <h2 class="text-sm font-medium text-gray-900 dark:text-white">{{ $t('settings.identityProvider.scimTitle') }}</h2>
          <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('settings.identityProvider.scimSubtitle') }}</p>
        </div>
        <svg class="w-4 h-4 text-gray-400 transition-transform flex-shrink-0" :class="scimOpen ? 'rotate-180' : ''" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" d="m19.5 8.25-7.5 7.5-7.5-7.5" />
        </svg>
      </button>

      <div v-show="scimOpen" class="px-4 pb-4 border-t border-gray-100 dark:border-gray-800 pt-4">
      <!-- Enterprise Gate for SCIM -->
      <template v-if="!hasFeature('scim')">
        <div class="rounded border border-gray-200 dark:border-gray-700 p-4 bg-gray-50 dark:bg-gray-900">
          <p class="text-xs text-gray-600 dark:text-gray-400 mb-2">
            {{ $t('settings.identityProvider.enterpriseScim') }}
          </p>
          <a
            href="https://docs.bagofwords.com/enterprise"
            target="_blank"
            rel="noopener noreferrer"
            class="text-xs text-blue-600 hover:text-blue-700"
          >
            {{ $t('settings.identityProvider.learnMore') }}
          </a>
        </div>
      </template>

      <template v-else>
        <!-- SCIM Endpoint URL -->
        <div class="mb-4 rounded border border-gray-200 dark:border-gray-700 p-3">
          <label class="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{{ $t('settings.identityProvider.scimBaseUrl') }}</label>
          <div class="flex items-center gap-2">
            <code class="flex-1 text-xs bg-gray-50 dark:bg-gray-900 px-2 py-1.5 rounded border border-gray-200 dark:border-gray-700 text-gray-700 dark:text-gray-300 font-mono">
              {{ scimBaseUrl }}
            </code>
            <button
              class="px-2 py-1.5 text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 border border-gray-200 dark:border-gray-700 rounded hover:border-gray-300"
              @click="copyToClipboard(scimBaseUrl)"
            >
              {{ copied === 'url' ? $t('settings.identityProvider.copied') : $t('settings.identityProvider.copy') }}
            </button>
          </div>
          <p class="text-[11px] text-gray-400 dark:text-gray-400 mt-1">{{ $t('settings.identityProvider.scimBaseUrlHint') }}</p>
        </div>

        <!-- Token Management -->
        <div class="mb-3 flex items-center justify-between">
          <label class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('settings.identityProvider.bearerTokens') }}</label>
          <button
            class="px-2 py-1 text-xs text-white bg-blue-600 rounded hover:bg-blue-700"
            @click="showCreateModal = true"
          >
            {{ $t('settings.identityProvider.generateToken') }}
          </button>
        </div>

        <!-- Loading State -->
        <div v-if="scimLoading" class="py-8 text-center">
          <div class="inline-block w-4 h-4 border-2 border-gray-200 dark:border-gray-700 border-t-gray-500 rounded-full animate-spin"></div>
        </div>

        <!-- Error State -->
        <div v-else-if="scimError" class="py-6 text-center text-xs text-red-500">
          {{ scimError }}
        </div>

        <!-- Tokens List -->
        <div v-else class="border border-gray-200 dark:border-gray-700 rounded overflow-hidden">
          <template v-if="tokens.length > 0">
            <div
              v-for="(token, idx) in tokens"
              :key="token.id"
              class="flex items-center px-3 py-2.5 text-xs"
              :class="{ 'border-t border-gray-100 dark:border-gray-800': idx > 0 }"
            >
              <span class="w-36 flex-shrink-0 text-gray-700 dark:text-gray-300 font-medium truncate">{{ token.name }}</span>
              <span class="w-36 flex-shrink-0 text-gray-400 dark:text-gray-400 font-mono text-[11px]">{{ token.token_prefix }}...</span>
              <span class="flex-1 text-gray-400 dark:text-gray-400 text-[11px]">
                <template v-if="token.last_used_at">
                  {{ $t('settings.identityProvider.lastUsed', { when: formatRelativeTime(token.last_used_at) }) }}
                </template>
                <template v-else>
                  {{ $t('settings.identityProvider.neverUsed') }}
                </template>
              </span>
              <span class="w-24 flex-shrink-0 text-gray-400 dark:text-gray-400 text-[11px]">
                {{ formatRelativeTime(token.created_at) }}
              </span>
              <button
                class="text-[11px] text-red-500 hover:text-red-700 ms-2"
                @click="confirmRevoke(token)"
              >
                {{ $t('settings.identityProvider.revoke') }}
              </button>
            </div>
          </template>

          <!-- Empty State -->
          <div v-else class="py-8 text-center">
            <p class="text-xs text-gray-400 dark:text-gray-400">{{ $t('settings.identityProvider.noTokens') }}</p>
            <p class="text-[11px] text-gray-400 dark:text-gray-400 mt-1">{{ $t('settings.identityProvider.noTokensHint') }}</p>
          </div>
        </div>
      </template>
      </div>
    </div>

    <!-- ================================================================== -->
    <!-- LDAP Directory Sync Section (collapsible)                          -->
    <!-- ================================================================== -->
    <div class="border border-gray-200 dark:border-gray-700 rounded-lg">
      <button
        class="w-full flex items-center justify-between px-4 py-3 text-left"
        @click="ldapOpen = !ldapOpen"
      >
        <div>
          <h2 class="text-sm font-medium text-gray-900 dark:text-white">{{ $t('settings.identityProvider.ldapTitle') }}</h2>
          <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('settings.identityProvider.ldapSubtitle') }}</p>
        </div>
        <svg class="w-4 h-4 text-gray-400 transition-transform flex-shrink-0" :class="ldapOpen ? 'rotate-180' : ''" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" d="m19.5 8.25-7.5 7.5-7.5-7.5" />
        </svg>
      </button>

      <div v-show="ldapOpen" class="px-4 pb-4 border-t border-gray-100 dark:border-gray-800 pt-4">
      <!-- Enterprise Gate for LDAP -->
      <template v-if="!hasFeature('ldap')">
        <div class="rounded border border-gray-200 dark:border-gray-700 p-4 bg-gray-50 dark:bg-gray-900">
          <p class="text-xs text-gray-600 dark:text-gray-400 mb-2">
            {{ $t('settings.identityProvider.enterpriseLdap') }}
          </p>
          <a
            href="https://docs.bagofwords.com/enterprise"
            target="_blank"
            rel="noopener noreferrer"
            class="text-xs text-blue-600 hover:text-blue-700"
          >
            {{ $t('settings.identityProvider.learnMore') }}
          </a>
        </div>
      </template>

      <template v-else>
        <!-- Not configured state -->
        <template v-if="!ldapStatus?.ldap_configured">
          <div class="rounded border border-dashed border-gray-300 dark:border-gray-600 p-6 text-center">
            <svg class="w-8 h-8 text-gray-300 dark:text-gray-600 mx-auto mb-2" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" d="M5.25 14.25h13.5m-13.5 0a3 3 0 0 1-3-3m3 3a3 3 0 1 0 0 6h13.5a3 3 0 1 0 0-6m-16.5-3a3 3 0 0 1 3-3h13.5a3 3 0 0 1 3 3m-19.5 0a4.5 4.5 0 0 1 .9-2.7L5.737 5.1a3.375 3.375 0 0 1 2.7-1.35h7.126c1.062 0 2.062.5 2.7 1.35l2.587 3.45a4.5 4.5 0 0 1 .9 2.7m0 0a3 3 0 0 1-3 3m0 3h.008v.008h-.008v-.008Zm0-6h.008v.008h-.008v-.008Zm-3 6h.008v.008h-.008v-.008Zm0-6h.008v.008h-.008v-.008Z" />
            </svg>
            <p class="text-xs text-gray-500 dark:text-gray-400 mb-1">{{ $t('settings.identityProvider.ldapNotConfigured') }}</p>
            <p class="text-[11px] text-gray-400 dark:text-gray-400">{{ $t('settings.identityProvider.ldapNotConfiguredHint') }}</p>
          </div>
        </template>

        <!-- Configured state -->
        <template v-else>
          <!-- Connection status -->
          <div class="rounded border border-gray-200 dark:border-gray-700 p-3 mb-4">
            <div class="flex items-center justify-between">
              <div>
                <div class="flex items-center gap-2">
                  <div
                    class="w-2 h-2 rounded-full"
                    :class="ldapTestResult?.connected ? 'bg-green-500' : (ldapTestResult ? 'bg-red-500' : 'bg-gray-300 dark:bg-gray-600')"
                  ></div>
                  <span class="text-xs font-medium text-gray-700 dark:text-gray-300">
                    {{ ldapTestResult?.connected ? $t('settings.identityProvider.statusConnected') : (ldapTestResult ? $t('settings.identityProvider.statusFailed') : $t('settings.identityProvider.statusNotTested')) }}
                  </span>
                </div>
                <p v-if="ldapTestResult?.connected" class="text-[11px] text-gray-400 dark:text-gray-400 mt-0.5 ms-4">
                  {{ ldapTestResult.server }}
                  <template v-if="ldapTestResult.vendor"> · {{ ldapTestResult.vendor }}</template>
                  <template v-if="ldapTestResult.user_count !== null"> · {{ ldapTestResult.user_count }} users</template>
                  <template v-if="ldapTestResult.group_count !== null"> · {{ ldapTestResult.group_count }} groups</template>
                </p>
                <p v-if="ldapTestResult && !ldapTestResult.connected" class="text-[11px] text-red-400 mt-0.5 ms-4">
                  {{ ldapTestResult.error }}
                </p>
              </div>
              <button
                class="px-2 py-1 text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 border border-gray-200 dark:border-gray-700 rounded hover:border-gray-300"
                :disabled="ldapLoading"
                @click="handleTestConnection"
              >
                {{ ldapLoading ? $t('settings.identityProvider.testing') : $t('settings.identityProvider.testConnection') }}
              </button>
            </div>
          </div>

          <!-- Last sync info -->
          <div v-if="ldapStatus?.last_sync" class="rounded border border-gray-200 dark:border-gray-700 p-3 mb-4">
            <div class="flex items-center justify-between">
              <div>
                <span class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('settings.identityProvider.lastSync') }}</span>
                <p class="text-[11px] text-gray-400 dark:text-gray-400 mt-0.5">
                  {{ ldapStatus.last_sync.timestamp ? formatRelativeTime(ldapStatus.last_sync.timestamp) : $t('settings.identityProvider.unknown') }}
                  {{ $t('settings.identityProvider.lastSyncDetail', {
                    gCreated: ldapStatus.last_sync.groups_created,
                    gUpdated: ldapStatus.last_sync.groups_updated,
                    gRemoved: ldapStatus.last_sync.groups_removed,
                    mAdded: ldapStatus.last_sync.memberships_added,
                    mRemoved: ldapStatus.last_sync.memberships_removed,
                  }) }}
                </p>
                <p v-if="ldapStatus.last_sync.errors.length" class="text-[11px] text-red-400 mt-0.5">
                  {{ $t('settings.identityProvider.errorCount', { n: ldapStatus.last_sync.errors.length, first: ldapStatus.last_sync.errors[0] }) }}
                </p>
              </div>
            </div>
          </div>

          <!-- Sync actions -->
          <div class="flex items-center gap-2">
            <button
              class="px-2 py-1.5 text-xs text-white bg-blue-600 rounded hover:bg-blue-700 disabled:opacity-50"
              :disabled="ldapLoading"
              @click="handleSync"
            >
              {{ ldapLoading ? $t('settings.identityProvider.syncing') : $t('settings.identityProvider.syncNow') }}
            </button>
            <button
              class="px-2 py-1.5 text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 border border-gray-200 dark:border-gray-700 rounded hover:border-gray-300 disabled:opacity-50"
              :disabled="ldapLoading"
              @click="handlePreview"
            >
              {{ $t('settings.identityProvider.previewChanges') }}
            </button>
          </div>

          <!-- Sync result flash -->
          <div v-if="lastSyncResult" class="mt-3 rounded border border-green-200 bg-green-50 dark:bg-green-950 p-3">
            <p class="text-xs text-green-700">
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
            </p>
          </div>

          <!-- Preview results -->
          <div v-if="ldapPreview" class="mt-3">
            <div class="rounded border border-gray-200 dark:border-gray-700 overflow-hidden">
              <div class="bg-gray-50 dark:bg-gray-900 px-3 py-2 border-b border-gray-200 dark:border-gray-700">
                <span class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('settings.identityProvider.preview') }} </span>
                <span class="text-xs text-gray-500 dark:text-gray-400">
                  {{ $t('settings.identityProvider.previewSummary', {
                    create: ldapPreview.groups_to_create,
                    update: ldapPreview.groups_to_update,
                    remove: ldapPreview.groups_to_remove,
                    changes: ldapPreview.total_membership_changes,
                  }) }}
                </span>
              </div>
              <div v-if="ldapPreview.groups.length" class="max-h-64 overflow-y-auto">
                <div
                  v-for="(group, idx) in ldapPreview.groups"
                  :key="group.dn"
                  class="flex items-center px-3 py-2 text-xs"
                  :class="{ 'border-t border-gray-100 dark:border-gray-800': idx > 0 }"
                >
                  <span class="flex-1 text-gray-700 dark:text-gray-300 truncate" :title="group.dn">{{ group.name }}</span>
                  <span class="w-20 text-gray-400 dark:text-gray-400 text-[11px]">{{ $t('settings.identityProvider.memberCount', { n: group.member_count }) }}</span>
                  <span class="w-24 text-[11px]" :class="group.exists_in_app ? 'text-gray-400 dark:text-gray-400' : 'text-blue-500'">
                    {{ group.exists_in_app ? $t('settings.identityProvider.groupExists') : $t('settings.identityProvider.groupNew') }}
                  </span>
                  <span v-if="group.members_to_add" class="text-[11px] text-green-600 me-2">+{{ group.members_to_add }}</span>
                  <span v-if="group.members_to_remove" class="text-[11px] text-red-500">-{{ group.members_to_remove }}</span>
                </div>
              </div>
              <div v-else class="py-4 text-center text-xs text-gray-400 dark:text-gray-400">
                {{ $t('settings.identityProvider.noLdapGroups') }}
              </div>
            </div>
          </div>

          <!-- LDAP Error -->
          <div v-if="ldapError" class="mt-3 text-xs text-red-500">
            {{ ldapError }}
          </div>
        </template>
      </template>
      </div>
    </div>

    <!-- ================================================================== -->
    <!-- SCIM Modals (Create + Revoke)                                      -->
    <!-- ================================================================== -->

    <!-- Create Token Modal -->
    <div v-if="showCreateModal" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50" @click.self="dismissCreateModal">
      <div class="bg-white dark:bg-gray-900 rounded-lg shadow-lg w-full max-w-sm p-4">
        <h3 class="text-sm font-medium text-gray-900 dark:text-white mb-3">{{ $t('settings.identityProvider.generateTokenTitle') }}</h3>

        <template v-if="!createdToken">
          <div class="mb-3">
            <label class="block text-xs text-gray-600 dark:text-gray-400 mb-1">{{ $t('settings.identityProvider.nameLabel') }}</label>
            <input
              v-model="newTokenName"
              type="text"
              :placeholder="$t('settings.identityProvider.namePlaceholder')"
              class="w-full px-2 py-1.5 text-xs border border-gray-200 dark:border-gray-700 rounded focus:outline-none focus:border-gray-400"
              @keydown.enter="handleCreateToken"
            />
          </div>
          <div class="flex justify-end gap-2">
            <button class="px-3 py-1.5 text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700" @click="dismissCreateModal">{{ $t('settings.identityProvider.cancel') }}</button>
            <button
              class="px-3 py-1.5 text-xs text-white bg-blue-600 rounded hover:bg-blue-700 disabled:opacity-50"
              :disabled="!newTokenName.trim() || creating"
              @click="handleCreateToken"
            >
              {{ creating ? $t('settings.identityProvider.generating') : $t('settings.identityProvider.generate') }}
            </button>
          </div>
        </template>

        <template v-else>
          <div class="rounded border border-amber-200 bg-amber-50 dark:bg-amber-950 p-3 mb-3">
            <div class="flex items-start gap-2">
              <svg class="w-4 h-4 text-amber-500 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126ZM12 15.75h.007v.008H12v-.008Z" />
              </svg>
              <p class="text-xs font-medium text-amber-800">{{ $t('settings.identityProvider.copyWarning') }}</p>
            </div>
          </div>
          <div class="flex items-center gap-2 mb-3">
            <code class="flex-1 text-[11px] bg-gray-50 dark:bg-gray-900 px-2 py-1.5 rounded border border-gray-200 dark:border-gray-700 text-gray-700 dark:text-gray-300 font-mono truncate">
              {{ createdToken }}
            </code>
            <button
              class="px-2 py-1.5 text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 border border-gray-200 dark:border-gray-700 rounded hover:border-gray-300 flex-shrink-0"
              @click="copyToClipboard(createdToken!, 'token')"
            >
              {{ copied === 'token' ? $t('settings.identityProvider.copied') : $t('settings.identityProvider.copy') }}
            </button>
          </div>
          <div class="flex justify-end">
            <button class="px-3 py-1.5 text-xs text-white bg-blue-600 rounded hover:bg-blue-700" @click="dismissCreateModal">{{ $t('settings.identityProvider.done') }}</button>
          </div>
        </template>
      </div>
    </div>

    <!-- Revoke Confirmation Modal -->
    <div v-if="tokenToRevoke" class="fixed inset-0 bg-black/50 flex items-center justify-center z-50" @click.self="tokenToRevoke = null">
      <div class="bg-white dark:bg-gray-900 rounded-lg shadow-lg w-full max-w-sm p-4">
        <h3 class="text-sm font-medium text-gray-900 dark:text-white mb-2">{{ $t('settings.identityProvider.revokeTitle') }}</h3>
        <p class="text-xs text-gray-600 dark:text-gray-400 mb-3">
          {{ $t('settings.identityProvider.revokeWarning', { name: tokenToRevoke.name }) }}
        </p>
        <div class="flex justify-end gap-2">
          <button class="px-3 py-1.5 text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700" @click="tokenToRevoke = null">{{ $t('settings.identityProvider.cancel') }}</button>
          <button class="px-3 py-1.5 text-xs text-white bg-red-600 rounded hover:bg-red-700" @click="handleRevoke">{{ $t('settings.identityProvider.revoke') }}</button>
        </div>
      </div>
    </div>

  </div>
</template>

<script setup lang="ts">
import { useScimTokens, type ScimToken } from '~/ee/composables/useScimTokens'
import { useLdapSync, type SyncResult as LDAPSyncResult } from '~/ee/composables/useLdapSync'
import { useEntraProfileSync, ENTRA_PROFILE_FIELDS } from '~/composables/useEntraProfileSync'

definePageMeta({
  auth: true,
  permissions: ['manage_identity_providers'],
  layout: 'settings'
})

const { t } = useI18n()
const { hasFeature, license } = useEnterprise()

// ── Collapsible section state (all collapsed by default) ──
const entraOpen = ref(false)
const scimOpen = ref(false)
const ldapOpen = ref(false)

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

// ── Entra ID profile sync ──
const {
  config: entraConfig,
  preview,
  previewLoading,
  saving: entraSaving,
  error: entraError,
  fetchConfig: fetchEntraConfig,
  save: saveEntra,
  fetchPreview: fetchEntraPreview,
} = useEntraProfileSync()

// Fields shown as default rows when nothing is configured yet.
const ENTRA_DEFAULT_VISIBLE = ['jobTitle', 'department', 'companyName', 'officeLocation']

const entraEnabled = ref(false)
const shownFields = ref<string[]>([...ENTRA_DEFAULT_VISIBLE])
const selected = ref<Set<string>>(new Set(ENTRA_DEFAULT_VISIBLE))
const fieldToAdd = ref('')
const entraSavedFlash = ref(false)
const hasFetchedEntra = ref(false)

const fieldLabel = (f: string) => {
  const key = `settings.identityProvider.field_${f}`
  const translated = t(key)
  return translated === key ? f : translated
}

const sampleValue = (f: string): string => {
  const v = preview.value?.samples?.[f]
  if (v === null || v === undefined || v === '') return ''
  if (typeof v === 'object') {
    return Object.entries(v)
      .filter(([, val]) => val !== null && val !== '')
      .map(([k, val]) => `${k}=${val}`)
      .join(', ')
  }
  return String(v)
}

// Allowlisted fields not currently shown → available to add.
const addableFields = computed(() =>
  (preview.value?.allowed_fields || ENTRA_PROFILE_FIELDS).filter(f => !shownFields.value.includes(f))
)

const toggleField = (f: string) => {
  const next = new Set(selected.value)
  if (next.has(f)) next.delete(f)
  else next.add(f)
  selected.value = next
}

const removeField = (f: string) => {
  shownFields.value = shownFields.value.filter(x => x !== f)
  const next = new Set(selected.value)
  next.delete(f)
  selected.value = next
}

const addField = () => {
  const f = fieldToAdd.value
  if (!f || shownFields.value.includes(f)) return
  shownFields.value = [...shownFields.value, f]
  selected.value = new Set([...selected.value, f])
  fieldToAdd.value = ''
}

const applyEntraConfig = () => {
  if (!entraConfig.value) return
  entraEnabled.value = !!entraConfig.value.enabled
  const configured = entraConfig.value.fields || []
  // Show configured fields plus the default set, so the admin always sees the
  // common attributes even before selecting any.
  const union = Array.from(new Set([...ENTRA_DEFAULT_VISIBLE, ...configured]))
  shownFields.value = union
  selected.value = new Set(configured.length ? configured : ENTRA_DEFAULT_VISIBLE)
}

const handleEntraSave = async () => {
  entraSavedFlash.value = false
  const fields = shownFields.value.filter(f => selected.value.has(f))
  const ok = await saveEntra({ enabled: entraEnabled.value, fields })
  if (ok) {
    applyEntraConfig()
    entraSavedFlash.value = true
    setTimeout(() => { entraSavedFlash.value = false }, 3000)
  }
}

const initEntra = async () => {
  if (hasFetchedEntra.value) return
  hasFetchedEntra.value = true
  await fetchEntraConfig()
  applyEntraConfig()
  // Live sample values from the admin's own profile (best-effort).
  fetchEntraPreview()
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

const _df = useFormatDate()
const formatRelativeTime = (timestamp: string | null) => {
  if (!timestamp) return ''
  const isoTimestamp = timestamp.endsWith('Z') ? timestamp : timestamp + 'Z'
  const date = new Date(isoTimestamp)
  const now = new Date()
  const diff = now.getTime() - date.getTime()
  const minutes = Math.floor(diff / 60000)
  const hours = Math.floor(diff / 3600000)
  const days = Math.floor(diff / 86400000)

  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  if (hours < 24) return `${hours}h ago`
  if (days < 7) return `${days}d ago`

  return _df.format(date, { month: 'short', day: 'numeric' })
}

// ── Init ──
// Entra profile sync is not enterprise-gated — load it immediately.
onMounted(() => { initEntra() })

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
