import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

// Run: node --experimental-strip-types tests/unit/agentQueriesNewQueryGate.mjs
// "New query" opens the editor that previews and saves caller-written code
// (POST /entities/preview, POST /entities). The backend refuses both without
// run_custom_code, so the button is shown only to users who hold it. Editing a
// saved query stays open to its editors: re-sending the stored code does not
// need the permission.

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')
const panel = read('../../components/entity/AgentQueriesPanel.vue')

const button = panel.match(/<button[^>]*data-testid="agent-query-new"[^>]*>/)
assert.ok(button, 'the panel has a New query button')
const gate = button[0].match(/v-if="([^"]+)"/)
assert.ok(gate, 'the New query button is gated')
const name = gate[1].trim()
assert.match(name, /^[A-Za-z_$][\w$]*$/, 'the gate is a single computed')

const script = panel.match(/<script[^>]*\bsetup\b[^>]*>([\s\S]*?)<\/script>/)[1]
assert.match(script, /import\s*\{[^}]*\buseCanRunCustomCode\b[^}]*\}\s*from\s*'~\/composables\/usePermissions'/,
  'the panel reads the run_custom_code permission')
assert.match(script, new RegExp(`const\\s+${name.replace('$', '\\$')}\\s*=\\s*useCanRunCustomCode\\(\\)`),
  'the gate is the run_custom_code permission')

// The editor itself is only mounted for create when the button is.
const modal = panel.match(/<EntityEditModal[^>]*:entity-id="null"[^>]*\/>/)
assert.ok(modal, 'the panel mounts the create editor')
assert.match(modal[0], new RegExp(`v-if="${name.replace('$', '\\$')}"`), 'the create editor is gated the same way')

const perms = read('../../composables/usePermissions.ts')
assert.match(perms, /export const useCanRunCustomCode = \(\) => computed\(\(\) => useCan\('run_custom_code'\)\)/,
  'useCanRunCustomCode is the run_custom_code permission')

console.log('agentQueriesNewQueryGate: ok')
