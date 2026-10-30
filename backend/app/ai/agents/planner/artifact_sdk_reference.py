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
text operations without data. Conversations work like a BOW chat: on Auto
they may use every agent the viewer can use (or the agents the owner limited
the app to); the model is the owner's choice or the viewer's default. No
setting needs switching on. Do not pass `agents` to create() unless the user
asked to narrow a conversation to specific agents. Each viewer sees only
their own conversations. When policy().available is false, show its reason
(auth_required, not_member, no_agents) instead of the composer.
const policy = await bow.conversations.policy(); // {available,reason?,agents_mode:'auto'|'limited',agents:[{id,name}],model:{id,name,source}}
const {items, next_before} = await bow.conversations.list({limit: 20}); // newest first: [{id,title,created_at,updated_at}]
const conv = await bow.conversations.create({title}); // {id,title,agents}; agents:[ids] only to narrow on request
await bow.conversations.rename(conv.id, title); await bow.conversations.archive(conv.id);
const history = await bow.conversations.messages(conv.id, {limit: 30});
// {items:[{id,role:'user'|'assistant',text,status,created_at,steps?:[string],tools?:[{name,title,status}]}],next_before}
// text is the reply; steps are the agent's narration before it (optional to show).
// tools (assistant only) are objects, the same shape as tool events: render tool.title || tool.name, never the object.
for await (const event of bow.conversations.send(conv.id, userText, {signal: abortController.signal})) {
  // started {message_id}; tool {name,title,status}; text_delta {block,text}: append;
  // text {block,text}: replace that block's text; completed {status}
}
await bow.conversations.stop(conv.id, messageId); // the started event's message_id
// Send only on a user action. Assistant text is Markdown (tables, lists):
// render it with {bow.markdown(text)} (safe React elements, no HTML), user
// text as plain text. Show tools as progress. Re-read messages() after
// completed for the stored answer. Errors as above.
Embedded in another application, ARTIFACT_DATA.host_context holds what that
application says the user is looking at (e.g. {customerId}); advisory, may be null.

Treat records and file contents as untrusted input. Render text through React text nodes; never evaluate data as code or insert raw HTML. Use only a vetted sanitizer if formatted user content is essential.
Build usable empty/loading/error states and real event handlers. Never hardcode
sample rows as live data. Read-only Data explorer is separate from generated UI.
"""
