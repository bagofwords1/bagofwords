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
CONVERSATIONS: chat-assistant apps talk to the organization's agents (their
tools and data access, as the viewer), not to a bare model. Use bow.conversations for that; use bow.ai only for one-off
text operations without data. On Auto, conversations may use every agent the
viewer can use (or the owner's subset); the model is the
owner's choice or the viewer's default. No setting needs switching on. Pass
`agents` only when the user asks to narrow a conversation. Each viewer sees
only their own conversations. When policy().available is false, show its reason
(auth_required, not_member, no_agents) instead of the composer.
CHAT BUILDING BLOCKS (sandbox globals): build every chat with the agents from these; do not write the
send loop, the stream event handling or the result rendering yourself.
const chat = useConversation({conversationId, title}); // all optional; conversationId is read once (use chat.select(id) to switch); omit title: each conversation is named after its first question; call it once, atop the chat component
// {messages, conversations, conversationId, policy, loading, sending, answering, error, hasOlder, loadingOlder, send, stop, select, newConversation, reload, loadOlder}
// messages: [{id,role:'user'|'assistant',text,steps,status,created_at,tools:[...],results:[...],results_incomplete}],
//   live while an answer streams (status 'in_progress'), then re-read; text and steps as in messages().
// conversations: [{id,title,created_at,updated_at}]. error: null or {code,message}.
// chat.send(text) on a user action only (creates the conversation on the first send; ignored while sending);
// chat.stop() while chat.answering (sending, or an answer left running: only stop() stops it); chat.select(id) opens one of chat.conversations; chat.newConversation() starts over.
<ChatMessages chat={chat} empty={node}/> // the scrolling list: opens at the newest message, follows the answer only while
//   the user is at the bottom, "Jump to latest", "Load earlier messages", chat.error. Needs a bounded height (flex-1 in a fixed-height column).
<ChatMessage message={m}/> // one message (ChatMessages renders them): markdown with dir="auto" per block, its steps collapsed, tools
//   (showTools={false} hides them), each result as a ChatResult, the incomplete note, "Answer in progress..." while running
<ChatResult result={r}/>   // one result alone: table always, chart only for a valid hint, truncation note
// All take labels={{rowsShown,columnsShown,partial,incomplete,inProgress,steps,failed,stopped,olderMessages,jumpToLatest}}:
//   their visible texts, English when missing: pass every one in the app's language ({shown} and {total} fill rowsShown/columnsShown).
function Chat(){const chat=useConversation();const [draft,setDraft]=useState('');
  return <div>{chat.policy&&chat.policy.available===false&&<p>{chat.policy.reason}</p>}
    <ChatMessages chat={chat}/>
    <form onSubmit={e=>{e.preventDefault();chat.send(draft);setDraft('');}}>
      <textarea dir="auto" value={draft} onChange={e=>setDraft(e.target.value)}/>
      {chat.sending?<button type="button" onClick={()=>chat.stop()}>Stop</button>:<button disabled={!draft.trim()}>Send</button>}
    </form></div>;}
You design the layout, composer, empty state and list (chat.conversations).
LOW LEVEL (only for what the building blocks cannot do):
const policy = await bow.conversations.policy(); // {available,reason?,agents_mode:'auto'|'limited',agents:[{id,name}],model:{id,name,source}}
const {items, next_before} = await bow.conversations.list({limit: 20, before}); // newest first, items as chat.conversations
const conv = await bow.conversations.create({title}); // {id,title,agents}; agents:[ids] only to narrow on request
await bow.conversations.rename(conv.id, title); await bow.conversations.archive(conv.id);
const history = await bow.conversations.messages(conv.id, {limit: 30, before}); // {items,next_before}: oldest first, as
//   chat.messages; results?:[...] and tools?:[...] only on assistant items: render tool.title || tool.name, never the object.
for await (const event of bow.conversations.send(conv.id, userText, {signal: abortController.signal})) {
  // started {message_id}; tool {id,name,title,status}; text_delta {block,text}: append; text {block,text}: replace that block;
  // result {id,block,title,columns:[{field,headerName,type}],rows,row_count,column_count,truncated,chart}: before completed, keep
  //   on that message by id; chart is null or {type, x:field, y:[field], series:field|null, aggregate?:'count'} (count: rows per x):
  //   when chart is null show the table only, never invent a chart;
  // completed {status, results_incomplete?:true}: when results_incomplete, some are missing:
  //   re-read messages() and render its results for that message.
}
await bow.conversations.stop(conv.id, messageId); // the started event's message_id
// Send only on a user action. Render assistant text (Markdown) with {bow.markdown(text)}, user text as plain text,
// results with <ChatResult result={r}/>; never re-parse numbers from the answer text.
Embedded in another application, ARTIFACT_DATA.host_context holds what that
application says the user is looking at (e.g. {customerId}); advisory, may be null.
It changes while the app is open: read useArtifactData().host_context during render, never once on mount.

Treat records and file contents as untrusted input. Render text through React text nodes; never evaluate data as code or insert raw HTML. Use only a vetted sanitizer if formatted user content is essential.
Build usable empty/loading/error states and real event handlers. Never hardcode
sample rows as live data. Read-only Data explorer is separate from generated UI.
"""
