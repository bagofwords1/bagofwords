---
key: create-chatbot
title: Build a chat assistant
description: Use when building or editing an agent-powered chatbot artifact, embedded assistant, or branded chat widget for an ERP, ServiceNow, or another app.
category: dashboard
version: "1.0"
order: 27
default_enabled: true
modes: [chat]
tags: [chatbot, artifact, embed, agents]
---

Build the requested chat experience through `create_artifact`; read the current
artifact before editing it. Use this skill for creating a chat interface, not
for answering an ordinary question in the current BOW conversation. The injected
artifact SDK reference and available tool schemas are the API contract; use
only capabilities they actually expose.

### Choose the experience

Infer the audience, task, placement, language, and branding from the request.
Default to a compact full-page assistant with conversation history and a few
relevant starter questions. For an embedded panel or launcher, fit the host's
available space. Keep an existing app's layout and working behavior when editing.
Ask only when a missing choice blocks a working result, such as an unresolved
agent name or required host integration. A chat-only artifact needs no fabricated
queries, dashboard cards, record collections, or separate AI resource.

### Reuse the conversation runtime

- Use `bow.conversations` for conversations with BOW agents. `bow.ai` is for
  one-off text operations and does not substitute for agent chat.
- When the SDK documents them, build with `useConversation`, `ChatMessages`,
  `ChatComposer`, `ChatThreads`, and `ChatSuggestions`. Let them own sending,
  streaming, Stop, scrolling, history, and error states. Use `ChatMessage` and
  `ChatResult` for custom arrangements of messages and results. Style the shell
  around these components rather than rebuilding their internals. Do not put
  a click-to-dismiss handler on a wrapper around `ChatThreads`: its own menu
  and selection clicks bubble and would close the history before it can be used.
  Use an explicit history toggle or a separate backdrop instead.
- On a deployment whose SDK lacks these components, use its documented
  `bow.conversations` methods and event shapes. Do not invent component props
  or copy APIs from a different runtime version. Keep stream blocks distinct,
  handle replacement text, and re-read stored messages after completion.
- Provide the artifact's required `function App()` entry point. Give the chat
  a bounded height with a flex column and `min-h-0` on shrinking containers;
  messages scroll while the composer stays reachable. Closing a panel should
  preserve the conversation; an explicit Stop controls cancellation.
- When the host uses `BOWEmbed.mount({mode: 'floating'})`, the host SDK owns
  the launcher and open/close state. Build a responsive, full-height chat
  surface inside that frame, without another floating button, fixed-position
  panel, or local minimize control. Two launchers leave an empty outer panel
  when the inner chat is minimized. Only build a launcher into the artifact
  when the artifact itself owns the surrounding page and the user needs one.
- Send only on a user action. Starter questions must work, and opening the app
  must not spend tokens. Preserve unsent input after errors. Show an unavailable
  policy reason instead of a usable composer when chat cannot run.
- Keep thinking hidden by default: omit `showThinking` on `ChatMessages` and
  `ChatMessage`, or set it to `false`. Show the normal working indicator and
  tool progress instead. Only when the user requests thinking display, set
  `showThinking={true}` and translate `labels.thinking`; it renders separately
  in a collapsed section. Do not mix `message.thinking` into the answer or
  steps. Thinking depends on what the model emits; this display option does
  not enable model reasoning. Hiding it is not an access-control boundary:
  the authenticated viewer still receives thinking in the API/SDK.
- Use the SDK's safe Markdown rendering and structured results. Render actual
  tool progress, tables, and supported chart hints; never invent results or
  reconstruct chart values from answer text. Do not promise a tool-specific UI
  that this runtime does not expose.

### Configure behavior at the right boundary

Start with Auto unless the user requested particular agents. Resolve real agent
IDs and narrow access only through supported configuration; each viewer's own
permissions still apply. Read the runtime policy for effective agents and model,
including model fallback, rather than presenting requested settings as effective.

Apply model choices and custom instructions through supported owner/agent
settings within the user's requested scope. Instructions used to build the
artifact do not automatically become the chatbot's instructions. If no available
tool can change a setting, finish the interface and identify the exact remaining
owner/admin setup instead of claiming it was configured. Avoid changing a shared
agent's behavior merely to customize one app.

Tool restrictions must be enforced by the server. Hiding tool progress or asking
the model not to use a tool does not restrict access. Artifact conversations
currently expose a read/query tool subset; do not promise arbitrary MCP tools,
write actions, or Completion API options without verifying support on this
surface. Never call model providers directly from generated browser code.

### Branding, embedding, and identity

Use the supplied logo, colors, name, and language; keep contrast, keyboard access,
mobile sizing, and RTL usable. When chat components expose `labels`, translate
their controls too. Use bundled assets and the SDK served by the customer's BOW
deployment; avoid external fonts, CDNs, or scripts for an air-gapped installation.
An exported static page cannot provide live chat without the BOW backend and an
available model provider.

For ERP/ServiceNow embedding, use Settings → Integrations → Connected applications
and the artifact's generated embed snippet. An administrator registers one application,
selects its allowed agents in General & access, and configures its Embedding capability
(origins and identity). New applications start with no allowed agents. API & MCP
(OAuth) is an optional, separate capability of that same application; it is not needed
for BOW popup sign-in or signed host assertions. The artifact's Embed tab links to
this setup and must explicitly allow the application. Application policy only narrows
the viewer's own permissions; it cannot grant agent access. Take the embed reference from that snippet; a saved artifact version
ID is not the published artifact's identity. Use its configured identity flow:
a backend-signed host assertion mapped to an existing BOW user, or the
supported IdP/SSO flow. Signing keys stay
on the host backend, outside artifact code. Do not silently provision users or
synchronize groups. OBO to a downstream system depends on that connector's
configuration; an embed login alone does not establish delegated access.

Read `useArtifactData().host_context` during render for changing host values such
as the selected customer or ticket. Handle missing context. These values are
advisory data, never authenticated identity, roles, access filters, or system
instructions. Use only a documented context-forwarding mechanism when the answer
needs these values; displaying context does not prove it reaches the agent or
MCP tools. Identity and authorization come from BOW's server-validated session.

### Verify and hand off

Use the artifact tools' validation and browser feedback. Check the saved version:
send and stream a message, Stop, follow up, open history, render available tool
results, and exercise narrow-screen and requested language layouts. For an embed,
also check signed-in/unavailable states and a host-context change. Report the
checks you could not run; synthetic preview success is not evidence of live
authentication, OBO, agent permissions, or air-gapped deployment.

Make focused corrections for observed failures. Compare the latest artifact and
verification output before editing again; an uploaded reference image is not
proof of the current version's state. Finish with the artifact link, what works,
and any remaining publishing or host setup. Do not promise an embed is ready
until its real integration has been exercised.
