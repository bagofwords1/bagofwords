// The Base / Scope grid in AgentFilesPanel belongs to path-scoped sources
// (S3, SharePoint, network dirs…). Live per-user sources — mail, Teams, Slack —
// have no path, so the grid's v-else must pair with the LIVE_TYPES check.
// A sibling v-if placed between the two (the Slack scope summary once was)
// silently re-pairs the v-else and shows "whole path" under mail and Teams.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const src = fs.readFileSync(new URL('../../components/datasources/AgentFilesPanel.vue', import.meta.url), 'utf8');

test('the Base / Scope grid pairs with the live-source check, nothing in between', () => {
  const live = src.indexOf('v-if="LIVE_TYPES.includes(conn.type)"');
  const grid = src.indexOf('<div v-else class="mt-3 grid');
  assert.ok(live > 0 && grid > live, 'live check and grid both present, in order');
  const between = src.slice(live, grid);
  // The live check opens a <template> that closes right before the v-else.
  assert.match(src.slice(live - 20, live), /<template\s*$/);
  assert.match(between, /<\/template>\s*$/);
  // Any v-if inside the live block is nested in it, not a sibling of the v-else.
  const afterTemplate = between.slice(between.lastIndexOf('</template>'));
  assert.doesNotMatch(afterTemplate, /v-if=/);
});

test('every live type, Slack included, is in LIVE_TYPES', () => {
  const m = src.match(/const LIVE_TYPES = \[([^\]]*)\]/);
  assert.ok(m);
  for (const t of ['outlook_mail', 'gmail_mail', 'teams_messages', 'slack_messages']) {
    assert.ok(m[1].includes(`'${t}'`), t);
  }
});
