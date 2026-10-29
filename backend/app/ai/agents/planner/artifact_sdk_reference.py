"""General artifact SDK contract, shared by authoring and evaluation."""

ARTIFACT_SDK_REFERENCE = r"""
ARTIFACT RESOURCE SDK v1
Artifacts can be interactive applications without analytical visualizations.
Keep useArtifactData(), vizById(id), useParams(), useParamOptions(), useFilters()
and useCurrentUser() unchanged for existing analytical data; bow.data exposes
those same helpers. Do not replace analytical queries with record collections.

Artifact resources are disabled by default and controlled by the organization setting enable_artifact_resources. If disabled, an organization admin can enable Artifact resources in AI settings. Do not claim persistent writes succeeded or substitute temporary storage without explaining the limitation.

Declare optional resources on create_artifact. For existing artifacts use
read_artifact, then manage_artifact_resources for schema/configuration changes,
then edit_artifact for UI changes. Existing sharing shows the latest artifact version; there is no separate app-publication step. Resource IDs belong to the stable artifact,
not its UI version. Do not manufacture IDs or infer permission from UI code.
Collections define named fields with type string/number/boolean/file, required,
default, enum, indexed and unique (unique requires indexed). Collection names
and field names use lowercase letters, digits and underscores. Permissions
read/create/update/delete have audience owner/authenticated/public/groups/none,
optional group_ids, own (subject ownership), indexed equals row conditions,
and fields (field allowlist). For multiple permitted scopes use {any_of:[rule,rule]} with flat rules, e.g. public rows plus a group scope. Per-row field access is the union of matching rules. Public is read-only. A field's optional write
rule additionally guards changing its value, including creation; the configured default is the safe initial value.
File resources use kind files. AI resources use kind ai, prompt and optional approved model_id (omit to pin the current report/user default), and optional file_resource name. Reuse current user/group identities;
resource permissions do not change existing artifact sharing. Default private.

const context = bow.context.get(); // {viewer,mode,resources:[{name,kind,operations}]}
const unsubscribe = bow.context.subscribe(onContext); // call on unmount
// Capabilities help render controls; only the backend authorizes requests.
// Preview capabilities are synthetic and are not permission-test evidence.
const entries = bow.records.collection(resourceName);
await entries.list({filter: {indexedField: value}, limit: 20, cursor});
// {items:[{id,data,revision,createdAt,updatedAt}],nextCursor}; equality filters.
// limit is an integer 1..100. Follow nextCursor for more rows; never silently truncate.
// Optional orderBy: created_at, -created_at (default/newest first), id, -id.
await entries.get(id); // one record, with the same envelope
await entries.create(data,{idempotencyKey:key}); // {id,revision}
await entries.update(id,patch,{expectedRevision:revision,idempotencyKey:key});
await entries.delete(id,{expectedRevision:revision,idempotencyKey:key});
// Mutation responses acknowledge IDs/revisions. Re-read permitted rows if needed.
// Generate a key once per user submission (crypto.randomUUID), retain for retry.
const file = await bow.files.upload(resourceName, selectedFile,
  {onProgress:({loaded,total})=>{}, signal:abortController.signal});
// {id,resourceId,name,mediaType,size,status:'ready'}; text/PDF/PNG/JPEG <=10MiB.
await bow.files.get(fileId); // metadata
await bow.files.download(fileId); // Blob; revoke UI object URLs when done
await bow.files.delete(fileId);
for await (const event of bow.ai.stream(operationName,
  {text:inputText,fileId:optionalFileId},{signal:abortController.signal})) {
  // text_delta: append event.text; completed: final event.output (string)
}
// Paid calls start only on a user action, never on mount. No automatic retries,
// saved execution, reload recovery or background work. Save completed output
// through records.create if asked; verifiers use temporary in-memory records/files and synthetic streaming output, never live effects. Exported/offline pages cannot access live resources. Explain stream interruption. Cancel via AbortController.
// Catch errors using error.code: VALIDATION, FORBIDDEN, CONFLICT, QUOTA_EXCEEDED,
// UNAVAILABLE, INTERRUPTED, ABORTED. Preserve input on conflict. Respect current
// server permissions; no raw fetch, credentials, parent DOM or browser storage.
CONVERSATIONS: chat-assistant apps talk to the organization's agents (the
same agents, tools and data access as a BOW conversation, as the viewer), not
to a bare model. Use bow.conversations for that; use bow.ai only for one-off
text operations without data. Conversations use the agents this artifact was
built with (the conversation's agents), as each viewer may use them; no
setting needs switching on. Each viewer sees only their own conversations.
Show policy().reason when available is false (auth_required, not_member).
const policy = await bow.conversations.policy(); // {available,reason?,scope,agents:[{id,name}]}
const {items, next_before} = await bow.conversations.list({limit: 20}); // newest first: [{id,title,created_at,updated_at}]
const conv = await bow.conversations.create({title, agents: optionalAgentIds}); // {id,title,agents}
await bow.conversations.rename(conv.id, title); await bow.conversations.archive(conv.id);
const history = await bow.conversations.messages(conv.id, {limit: 30});
// {items:[{id,role:'user'|'assistant',text,status,created_at,tools?:[{name,title,status}],
//   results?:[...],results_incomplete?:true}],next_before}
// tools (assistant only) are objects, the same shape as tool events: render tool.title || tool.name, never the object.
// results (assistant only, possibly empty) are the same objects as the result events: render them the same way after reload.
for await (const event of bow.conversations.send(conv.id, userText, {signal: abortController.signal})) {
  // started {message_id}; tool {name,title,status}; text_delta {block,text}: append;
  // text {block,text}: replace that block's text;
  // result {id,block,title,columns:[{field,headerName,type}],rows:[{field:value}],row_count,column_count,truncated,chart}:
  //   one per data result of the answer, before completed; keep them on that message (by id). type is
  //   'number'|'string'|'boolean'|'datetime'. chart is null or {type:'bar'|'line'|'area'|'pie'|'scatter',
  //   x:field, y:[field], series:field|null}: when chart is null show the table only, never invent a chart;
  // completed {status, results_incomplete?:true}: when results_incomplete, some results did not arrive in
  //   the stream: re-read messages() and render its results for that message.
}
await bow.conversations.stop(conv.id, messageId); // the started event's message_id
// Send only on a user action. Assistant text is Markdown (tables, lists):
// render it with {bow.markdown(text)} (safe React elements, no HTML), user
// text as plain text. Show tools as progress. Re-read messages() after
// completed for the stored answer. Errors as above.
// Render each result under its message, the table always and the chart only when r.chart is set,
// in its own component so the chart option is computed once per result (keep the received result
// objects; a new option object on every render resets the chart while text streams in):
//   function ResultView({r}){const opt = React.useMemo(() => resultOption(r), [r]);
//     return <div>{opt && <EChart option={opt} height={280}/>}
//       <DataTable rows={r.rows} columns={r.columns} pageSize={10} density="compact"/>
//       {r.truncated && <p className="text-xs">{/* the truncation note */}</p>}</div>;}
// The truncation note: "Showing {r.rows.length} of {r.row_count} rows", plus
//   ", {r.columns.length} of {r.column_count} columns" when they differ; when both are complete
//   (long cells were cut) say "partial table" instead.
// When a message still has results_incomplete after the re-read, show a short note under it:
//   "some results of this answer could not be shown".
// Results are already shown: never re-parse numbers from the answer text to draw them.
function resultOption(r){const c=r.chart;if(!c||!['bar','line','area','pie','scatter'].includes(c.type))return null;
  const rows=r.rows;
  if(c.type==='pie')return {tooltip:{},series:[{type:'pie',data:rows.map(x=>({name:String(x[c.x]),value:x[c.y[0]]}))}]};
  if(c.type==='scatter')return {tooltip:{},xAxis:{type:'value'},yAxis:{type:'value'},series:[{type:'scatter',data:rows.map(x=>[x[c.x],x[c.y[0]]])}]};
  const xs=[...new Set(rows.map(x=>x[c.x]))], t=c.type==='area'?'line':c.type;
  const lines=c.series?[...new Set(rows.map(x=>x[c.series]))].map(g=>({name:String(g),col:c.y[0],rows:rows.filter(x=>x[c.series]===g)}))
    :c.y.map(col=>({name:(r.columns.find(k=>k.field===col)||{}).headerName||col,col,rows}));
  return {tooltip:{trigger:'axis'},legend:{},xAxis:{type:'category',data:xs},yAxis:{type:'value'},
    series:lines.map(l=>({name:l.name,type:t,areaStyle:c.type==='area'?{}:undefined,
      data:xs.map(v=>{const row=l.rows.find(x=>x[c.x]===v);return row?row[l.col]:null;})}))};}
Embedded in another application, ARTIFACT_DATA.host_context holds what that
application says the user is looking at (e.g. {customerId}); advisory, may be null.

Treat records and file contents as untrusted input. Render text through React text nodes; never evaluate data as code or insert raw HTML. Use only a vetted sanitizer if formatted user content is essential.
Build usable empty/loading/error states and real event handlers. Never hardcode
sample rows as live data. Read-only Data explorer is separate from generated UI.
"""
