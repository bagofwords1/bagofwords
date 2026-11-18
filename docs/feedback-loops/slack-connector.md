# Feedback Loop — "Connectors: add Slack as a data source (read channels, threads and search messages)"

Goal: an agent can answer from Slack the way it already answers from Microsoft
Teams. Example questions are *"what did customers complain about in #support
this week?"* and *"find messages mentioning X"*. The agent reads **live, as the
signed-in user**: nothing is copied or indexed, and each person sees only the
channels and DMs they belong to. On top of the Teams model, each **agent** can
be narrowed to the Slack channels it is about.

## What was built

| Layer | Where |
|---|---|
| Slack Web API client: list, read, search, threads | `backend/app/data_sources/clients/slack_messages_client.py` |
| Registry entry `slack_messages` (per-user, integration form, `dev_only`) | `backend/app/schemas/data_source_registry.py` |
| Config + credentials schemas | `backend/app/schemas/data_sources/configs.py` (`SlackMessagesConfig`, `SlackCredentials`) |
| "Sign in with Slack": user scopes, `authed_user` token, flat refresh | `backend/app/services/connection_oauth_service.py` (`get_oauth_params`, `normalize_slack_token_response`), `backend/app/routes/connection_oauth.py` (`user_scope`) |
| Per-agent channel scope: model, migration, service | `backend/app/models/data_source_connection_setting.py`, `backend/alembic/versions/slackchan01_data_source_connection_settings.py`, `backend/app/services/slack_channel_scope.py` |
| Scope endpoints: picker list, read, save | `backend/app/routes/data_source.py` (`/connections/{id}/conversations`, `/channel-scope`) |
| Scope applied at runtime | `_file_tool_common.py` (`resolve_file_client`), the browse routes in `routes/data_source.py`, attach in `routes/file.py` |
| Planner context: chat vocabulary, per-agent scope note | `tables_schema_section.py`, `schema_context_builder.py` (`_stamp_slack_scopes`) |
| Chat tools: per-row `link:`, a hint when the query is a channel name | `backend/app/ai/tools/implementations/chat_tools.py` |
| Wiring into the hardcoded chat-type lists (backend + frontend) | `agent_roster.py`, `connection_catalog_common.py`, `_file_tool_common.py`, `schema_context_builder.py`, `routes/data_source.py`, `data_source_service.py`, and 8 frontend components/composables |
| Channel picker UI | `frontend/components/datasources/SlackChannelPicker.vue`, `AgentFilesPanel.vue`, `locales/*.json` (`slackChannels.*`) |

Ids are self-describing: `slack:<channelId>` and `slack:<channelId>/<ts>`.
This means a search hit can be read without any lookup state.

## Design decisions

- **A connector, not an MCP preset.** This matches Teams: the same chat tools,
  per-user OAuth, and control over links, names, errors and rate limits.
  Slack's hosted MCP server needs a Marketplace or internal app, admin approval,
  and no DCR. It remains a possible follow-up.
- **The user token comes from `authed_user`.** Scopes are sent as `user_scope`;
  sent as `scope`, Slack would mint a bot token. A bot token only sees channels
  it was invited to and cannot call `search.messages`.
- **Each customer registers their own internal Slack app.** Its client id and
  secret go on the connection. An internal app is exempt from Slack's tighter
  history limits for apps distributed outside the Marketplace.
- **Teams parity:**
  - A channel listing includes each thread's replies (`conversations.replies`,
    bounded).
  - An empty plain-language search is retried word by word.
  - Every search hit carries its thread context.
- **Per-agent channel scope.** It is stored by Slack channel **id**, so renaming
  a channel never breaks it. Rules:
  - Channels and DMs are scoped **independently**.
  - A report with several agents on the same connection reads the **union** of
    their scopes.
  - If the report **pins** an agent on that connection, only pinned agents
    count. A personal connector that joins every run by itself (see
    `resolve_run_agents`) cannot reopen a scope the user picked.
  - The scope only narrows what the user's own Slack membership allows. It is a
    focus setting, not a grant.
  - It applies to every client built for the agent: the chat tools, browse
    and attach, and `DataSourceService.construct_clients` (the clients a run
    hands to generated code), so no path reads past it.
  - **Private channel names stay private.** Each channel's name and privacy
    are read from the saving manager's own Slack, never from the request.
    Anyone who can view the agent reads its choice, so a private channel
    comes back unnamed and the planner note counts private channels without
    naming them. A channel whose privacy is unknown counts as private.
- **Search with a scope:**
  - With ≤10 channels and DMs off, the scope is sent to Slack as `in:#a in:#b`.
    This was verified live: Slack ORs repeated `in:` filters.
  - Otherwise, or if that comes back empty, the search runs workspace-wide and
    is filtered by channel id. When the pages walked hold no in-scope match
    but more pages exist, the cursor is returned (the result says
    INCOMPLETE). It is not replaced by a word-by-word search.
- **Paging is stable.** Both listings (one conversation, or the fan-out over
  many) freeze the window's upper bound in the cursor. The fan-out expands
  the newest threads over all scanned conversations, chosen
  deterministically, so every page is built from the same rows.
- **DM partners are named lazily.** Only the DMs a call actually shows cost a
  `users.info` lookup, and names are cached for an hour across clients in
  the process (a busy user's 400 DMs no longer hit 429 on every call).

## Setting up the Slack app (admin, once per workspace)

1. At <https://api.slack.com/apps>, choose **Create New App**, then **Paste a
   manifest**, then pick the workspace. Paste this manifest (or use **From
   scratch** and set the same values by hand):

   ```json
   {
     "display_information": {"name": "Bag of Words"},
     "oauth_config": {
       "redirect_urls": ["https://<your-bow-address>/api/connections/oauth/callback"],
       "scopes": {"user": ["channels:read", "groups:read", "channels:history", "groups:history",
                           "search:read", "users:read", "im:read", "im:history",
                           "mpim:read", "mpim:history"]}
     },
     "settings": {"org_deploy_enabled": false, "socket_mode_enabled": false,
                  "token_rotation_enabled": false}
   }
   ```

   The scopes are **user** scopes and all read-only; there are no bot scopes.
   Keep the app internal and don't distribute it. Token rotation can stay off;
   if it is on, the 12-hour refresh is handled.
2. From **Basic Information**, copy the Client ID and Client Secret into a new
   BOW connection: **Add connection**, then **Slack**.
3. Each person clicks **Sign in with Slack** on the connection.
4. Optional: on an agent's **Files** tab, use **Channels** to pick the channels
   that agent reads, and whether it also reads each person's DMs.

## Loop A — deterministic (no Slack workspace)

```bash
cd backend
uv run pytest tests/unit/test_slack_messages_client.py tests/unit/test_slack_channel_scope.py \
  tests/unit/test_slack_schema_context.py -q
uv run pytest tests/e2e/test_slack_messages_connector.py tests/e2e/test_slack_connection_oauth.py \
  -m e2e --db=sqlite -q
# Same e2e set against PostgreSQL (any empty database):
TEST_DATABASE_URL=postgresql://…/empty_db uv run pytest tests/e2e/test_slack_*.py --db=external -q
```

`FakeSlack`, in the unit tests, implements the documented Web API behaviour:

- **Shape of answers:**
  - Errors come back as HTTP 200 with `ok: false`.
  - Pagination uses `cursor` / `next_cursor`.
  - A 429 carries `Retry-After`.
  - `x-oauth-scopes` lists the token's scopes.
- **Endpoints:**
  - `users.conversations` returns memberships only.
  - `conversations.history` returns top-level messages, with thread replies
    under `conversations.replies`.
  - `search.messages` returns a `permalink` per match, with `thread_ts` in the
    permalink for replies.

The tests pin, among others:

- **Reading:** membership-only listing; thread replies listed under their root,
  with no duplicate for "also send to channel".
- **Search:** word-by-word broadening; hit context; the channel-name hint.
- **Errors and limits:** 429 retry and give-up; friendly `not_in_channel` and
  revoked-token errors.
- **OAuth:** `authed_user` and the flat refresh token.
- **Channel scope:**
  - Union, and precedence of pinned agents.
  - A renamed channel stays in scope (by id).
  - The scope is applied in browse and attach paths.
  - Saving is ids only, managers only.
  - It is cleaned up on unlink and when the connection is deleted.

## Loop B — live (real workspace, Claude Haiku 4.5 / Opus)

This ran against a real Slack workspace through the local stack (backend +
`yarn devs`, HTTPS), with an internal app created from the manifest above. No
tokens appear here. All of the following was confirmed in the backend log by
the Slack API calls made:

1. **OAuth.** `oauth.v2.access` returned 200, so PKCE was accepted. Then
   `auth.test` and `users.conversations` succeeded and the credentials were
   saved.
2. **"Summarize #channel from the last 7 days."** One `conversations.history`
   call with `oldest`, plus `conversations.replies` for the threaded posts. The
   result was a correct, dated summary.
3. **"Find messages mentioning X."** `search.messages` with `in:#channel`,
   carrying hit context. A plain question with no channel found the right
   discussion.
4. **Channel scope.** An agent scoped to two channels with DMs off:
   - Every Slack call (history, replies, search) went to those two channel ids
     only.
   - The search was sent as `… in:#a in:#b` and returned matches from both, so
     Slack ORs the filters.
   - An unscoped personal connector on the same connection did not widen it.

Screenshots (seeded channel names only; customer-named channels removed):
`media/pr/slack-connector/`.

## Known limitations (deliberate)

- **The @-mention file picker** works at the connection level, with no agent,
  so an agent's channel scope does not apply there. It is the user's explicit
  pick from their own Slack.
- **Search with more than 10 channels in scope, or DMs plus a channel scope,**
  runs workspace-wide (up to 10 pages × 100) and is then filtered by id. Very
  common words can push in-scope matches out of that window.
- **"Last 7 days" listing reads threads whose root is in the window.** New
  replies on an older root are not listed; Teams does include them. Search
  still finds them.
- **`users.conversations` is capped at 2,000 conversations** and excludes
  archived channels.
- **Each page of a fan-out listing re-reads** the scanned conversations
  (about 20 `conversations.history` calls and up to 10 `conversations.replies`
  calls). The pages are consistent, but each page costs the full fan-out.
  Passing `conversation` pages through one channel cheaply.
- **Search date filters are padded by two days** on each side, because Slack
  may read `after:` / `before:` in the user's own timezone. Each match is then
  filtered exactly by its timestamp.
- **`search.messages` is a legacy method** (Slack recommends
  `assistant.search.context`). Moving would be a change to that one function.
- **Enterprise Grid** (several workspaces) has not been tested.
