// MCP connection to the mock MCP Apps server + an agent carrying it (API).
const { launch, api } = require('./common');
const fs = require('fs');
(async () => {
  const { browser, page } = await launch();
  await page.goto('http://localhost:3000/settings/models', { waitUntil: 'commit', timeout: 180000 });
  await page.waitForTimeout(3000);
  const conn = await api(page, 'POST', '/api/connections', {
    name: 'Regional Sales', type: 'mcp',
    config: { server_url: 'http://127.0.0.1:3340/mcp', transport: 'streamable_http' },
    credentials: {}, auth_policy: 'system_only',
  });
  console.log('connection', conn.status, conn.body && (conn.body.id || JSON.stringify(conn.body).slice(0, 300)));
  const id = conn.body.id;
  const tools = await api(page, 'POST', `/api/connections/${id}/refresh-tools`);
  console.log('tools', tools.status, (tools.body || []).map(t => t.name + ':' + t.policy).join(', '));
  const ds = await api(page, 'POST', '/api/data_sources', {
    name: 'Sales Ops', connection_ids: [id], generate_summary: false,
    generate_conversation_starters: false, generate_ai_rules: false, use_llm_sync: false, is_public: true,
  });
  console.log('agent', ds.status, ds.body && ds.body.id);
  fs.writeFileSync(__dirname + '/ids.json', JSON.stringify({ connection_id: id, data_source_id: ds.body && ds.body.id, org: conn.org }));
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
