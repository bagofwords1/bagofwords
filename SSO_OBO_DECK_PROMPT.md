# Prompt — Build customer-facing deck for BOW × Entra ID delegated authentication

Paste the contents below into a fresh Claude (or any LLM) session. It is fully
self-contained — the reader needs zero prior context about the product.

---

## Your task

Build a **simple, clean customer-facing slide deck** that explains three related
authentication flows in a product called **Bag of Words (BOW)** when it's
deployed against Microsoft Entra ID. The deck is for a **non-engineer audience**
— a customer's IT lead, data platform owner, or security reviewer. The tone is
**consultative and plain-English**: no Microsoft acronyms unless absolutely
necessary, no code, no grant-type strings, no OAuth spec references. Each slide
should be immediately understandable without the speaker talking.

## Deliverable

One of the following, listed in order of preference. Pick whichever you can
produce confidently:

1. A **`.pptx` file** built with `python-pptx`. Use box-and-arrow diagrams drawn
   with `add_shape` and connectors. Don't rely on external images.
2. A **single self-contained HTML file** with inline SVG diagrams, styled as a
   deck (one slide per full viewport height, presenter navigates with arrow keys
   via a tiny bit of JS). No CDN dependencies.
3. A **Marp-compatible markdown file** (`slides.md`) with fenced mermaid blocks
   for diagrams, plus a one-line instruction on how to render it with
   `npx @marp-team/marp-cli slides.md -o slides.pdf --mermaid`.

Whichever format you pick, keep it to **roughly 12 slides**. More is worse.

## Product context (background — do not put this on slides verbatim)

- **BOW** is a self-hosted AI analyst: users chat with their data sources,
  BOW's LLM generates SQL/DAX and returns charts and tables.
- Data sources include Microsoft Fabric (SQL warehouses + lakehouses),
  Power BI semantic models, BigQuery, Snowflake, Salesforce, and others.
- Historically BOW connected to each data source with **one shared service
  principal** per connection. Every BOW user saw whatever that service
  principal could see. Row-level security, workspace roles, table GRANTs at the
  data-source level were effectively bypassed.
- The new feature lets BOW connect to each data source **as the individual
  end-user instead of as a shared service principal**. The user's real Entra /
  Fabric / PowerBI permissions are enforced by the data source itself.
- BOW admins flip a per-connection toggle called **"Require user
  authentication"** to enable this mode for a given connection.
- There are **three distinct authentication moments** in a deployment; the deck
  needs to explain all three and make clear they are not the same thing:
  1. **App login** — how a user signs into the BOW web app. Configured once by
     the BOW admin at deployment time.
  2. **Flow A — OBO (On-Behalf-Of)** — for Microsoft-native data sources
     (Fabric, Power BI, Azure SQL), BOW silently exchanges the user's app-login
     token for a data-source-scoped token at login time. The user clicks
     nothing. When they enter BOW, every Entra-backed connection is already
     connected as them.
  3. **Flow B — Explicit per-connection sign-in** — for anything OAuth (Google
     BigQuery, Snowflake, Salesforce, NetSuite, and also a Microsoft fallback
     when OBO isn't configured), the user clicks a **"Sign in with X"** button
     on each connection once. Standard OAuth 2.0 with PKCE. BOW stores a
     refresh token and renews silently after that.
- Under the hood BOW stores per-user delegated tokens in an encrypted table
  and also keeps a per-user "overlay" of which tables/columns that user can
  actually see, refreshed every time their token is used. When an admin revokes
  a user upstream (e.g., removes them from a Fabric role), BOW's next sync
  marks the affected tables as revoked so they disappear from the user's
  experience. **Put the overlay idea on a slide in plain language** — customers
  love this, it's the main product differentiator vs. "just use RLS".

## Slide-by-slide brief

Build the slides in this order. Each entry below has: **Title**, **What goes
on it**, **Diagram** (draw this; don't skip). Keep body copy to at most ~30
words per slide — the diagrams do the work.

### 1. Title slide

- **Title:** "Delegated data access for your BOW users"
- **Subtitle:** "One identity, from login to data. Entra-native."
- Plain background. Customer / BOW logo placeholder top-left.

### 2. The problem today

- **Title:** "Today, BOW sees everything as one service account."
- **Body (≤30 words):** "Your users all share one service principal. BOW
  decides who sees what. Your Fabric / PowerBI permissions are bypassed.
  Audits can't tell users apart."
- **Diagram:** Three user icons on the left → single BOW box in the middle →
  one arrow labelled **"service principal token"** → a cylinder labelled
  **"Fabric / Power BI"** → all users see the same full set of tables.
  Caption under the arrow in red: **"One token for everyone."**

### 3. The shift

- **Title:** "We want BOW to carry the user's identity all the way."
- **Body (≤30 words):** "Each user queries the data source as themselves.
  Your existing Entra groups, workspace roles, and row-level security do the
  enforcement — unchanged."
- **Diagram:** Same three users on the left → a BOW box → **three separate
  arrows** labelled "alice's token", "bob's token", "carol's token" → the
  Fabric / Power BI cylinder → three smaller "subset" clusters, each labelled
  with one user's name, showing that each user sees a different subset.

### 4. Three auth moments — don't conflate them

- **Title:** "Three sign-ins — one of them is visible to your users."
- **Body:** A short intro: "There are three distinct authentication steps in
  a BOW deployment. Only one of them is something your users do themselves."
- **Diagram:** A horizontal three-card layout. Each card has a big number,
  a name, and a one-line description.
  - **Card 1 — App login.** "User signs into BOW with Entra ID. Once per
    session."
  - **Card 2 — Flow A: OBO (silent).** "BOW picks up Fabric / Power BI
    automatically at login. Zero clicks."
  - **Card 3 — Flow B: Per-connection sign-in.** "One click per non-Microsoft
    data source, first time only."
- Under the diagram, a small note: "The next three slides go through each in
  order."

### 5. Moment 1 — App login (Entra ID SSO into BOW)

- **Title:** "App login — how users get into BOW"
- **Body (≤30 words):** "BOW is configured as an application in your Entra
  tenant. Users click 'Sign in with Microsoft' on the BOW login page. Same
  experience as any Microsoft-integrated SaaS app."
- **Diagram:** Horizontal sequence, 4 boxes left-to-right connected by arrows:
  1. **User's browser** →
  2. **BOW login page** (arrow: "click Sign in with Microsoft") →
  3. **Entra ID login** (arrow: "authenticate, consent") →
  4. **BOW dashboard** (arrow back: "redirect + session cookie").
  Small callout box below: **"Your BOW admin registers BOW as an Entra app
  once. Group membership flows in automatically."**

### 6. Moment 2 — Flow A (OBO) — silent data-source auth for Microsoft

- **Title:** "Flow A — zero-click for Microsoft data sources"
- **Body (≤30 words):** "During app login, BOW silently exchanges the user's
  Entra token for a Fabric-scoped and a Power BI-scoped token. By the time
  the user lands in BOW, every Microsoft data source is already connected as
  them."
- **Diagram:** Vertical flow with 5 steps. Label the arrows.
  1. User completes Entra login (from slide 5) →
  2. BOW receives the user's access token →
  3. BOW asks Entra (arrow: "give me a Power BI token for this user") →
  4. Entra returns a Power BI / Fabric token (arrow: "delegated token,
     scoped to this user") →
  5. BOW stores it encrypted, queries Power BI / Fabric as the user.
  Footer note: **"No extra clicks. No extra consent prompts. No passwords
  re-entered."**

### 7. Moment 3 — Flow B — explicit per-connection sign-in

- **Title:** "Flow B — one click per non-Microsoft connection"
- **Body (≤30 words):** "For Google, Snowflake, Salesforce, NetSuite (and
  as a Microsoft fallback when OBO isn't configured), the user clicks a
  'Sign in with X' button on the connection once. BOW then refreshes their
  token silently forever."
- **Diagram:** Vertical flow with 5 steps.
  1. User opens a data connection in BOW → sees **"Sign in with Google"**
     button →
  2. Clicks it; browser is redirected to Google login (note: "already signed
     in? → invisible") →
  3. User consents (first time only) →
  4. Google redirects back to BOW with an authorization code →
  5. BOW exchanges the code for an access + refresh token, stores both.
  Footer note: **"Once per connection, first time only. After that, silent
  renewal."**

### 8. Side-by-side: Flow A vs Flow B

- **Title:** "When to use which"
- **Content:** A clean 2-column comparison table. Rows:

  | | Flow A (OBO) | Flow B (explicit) |
  |---|---|---|
  | User clicks required | 0 | 1 per connection, first time |
  | Works with | Microsoft (Fabric, Power BI, Azure SQL) | Any OAuth provider |
  | Identity provider | Entra ID | Any (Microsoft, Google, Snowflake, …) |
  | Setup | Entra app + delegated API permissions | OAuth app per provider |
  | Renewal | Silent | Silent |

- **Footer:** "Most customers enable both. Flow A handles their Microsoft
  stack; Flow B covers everything else."

### 9. What users actually see

- **Title:** "What the user sees"
- Build two side-by-side phone/browser mockups (rough rectangles with labels
  are fine — no need for real UI screenshots):
  - **Left — Microsoft-only customer:**
    1. BOW login → Sign in with Microsoft → Dashboard.
    2. Every data source is already connected. They start chatting with
       their data immediately.
  - **Right — Mixed customer:**
    1. BOW login → Sign in with Microsoft → Dashboard.
    2. Fabric + Power BI already connected (Flow A).
    3. BigQuery card shows a **"Sign in with Google"** button; Snowflake
       card shows **"Sign in with Snowflake"**. One click each, first time
       only.

### 10. Permission changes — the overlay story

- **Title:** "When you change someone's permissions, BOW follows."
- **Body:** "Every time BOW refreshes a user's schema, it asks the data
  source **as that user**. Whatever the user can see is what BOW shows them.
  Whatever they lose access to is removed from their BOW experience on the
  next sync — automatically."
- **Diagram:** Timeline with 3 stages left-to-right:
  1. **Monday.** Alice is in `Finance` group → her BOW overlay includes
     `finance` table, `hr` table.
  2. **Tuesday.** Admin removes Alice from `Finance` group in Entra.
  3. **Wednesday (next sync).** Alice's BOW overlay: `finance` marked
     **revoked**, excluded from her LLM context, hidden in her UI. `hr`
     still visible.
  Footer note: **"One place to manage access: Entra / Fabric / Power BI.
  BOW mirrors."**

### 11. What you need to set up

- **Title:** "What we need from your side"
- A simple 2-column split:
  - **For app login + Flow A (Microsoft):**
    - An Entra app registration for BOW
    - Redirect URI: `https://<your-bow>/api/auth/entra/callback`
    - Delegated API permissions: **Power BI Service**, **Fabric** (as needed)
    - A client secret
  - **For Flow B (other providers):**
    - An OAuth client per provider (Google Cloud, Snowflake, Salesforce, …)
    - Redirect URI: `https://<your-bow>/api/connections/oauth/callback`
- Footer: **"Your identity admins stay in control. BOW never sees
  passwords."**

### 12. Recommendation + next steps

- **Title:** "Recommendation"
- Three bullets:
  - "If you're all-Microsoft → enable **Flow A**, done."
  - "If you have non-Microsoft sources → enable **Flow B** alongside."
  - "Keep the old shared-service-principal mode on connections where
    per-user identity isn't the goal (e.g., public reporting)."
- A final box: **"Next steps — a 30-minute technical session with your IT
  to register the Entra app and one other provider. We'll bring a checklist."**

## Design rules

- **Palette.** Max three colours. Suggestion: a neutral slate grey for
  boxes, a soft blue for the happy-path arrows, a red-orange for the
  "today's problem" slide only. White background.
- **Fonts.** Inter or Helvetica / Arial fallback. One bold weight for
  titles, one regular for body, one mono (Menlo / Consolas) for code-ish
  labels inside boxes if you need them.
- **Type sizes (PPTX pt).** Title 32–36, body 18–20, diagram labels 14.
- **Spacing.** Generous margins. Plenty of whitespace around diagrams.
- **No bullet indent nesting.** One level only.
- **Arrows.** Thin, with a small arrowhead. Label *above* the arrow in one
  or two words, never a sentence.
- **Icons.** Skip them entirely, or use very simple outline shapes (person
  = circle + rounded-rectangle torso). No clipart, no emoji.
- **No "designed by Claude" watermark, no decoration, no gradients.**

## Tone guardrails (what NOT to put on slides)

- Never write "OBO", "jwt-bearer", "authorization code", "PKCE",
  "on_behalf_of", or any grant-type string on a slide. These are IT
  appendix material.
- Never write "UserConnectionCredentials", "UserDataSourceOverlay", or any
  BOW internal table/class name.
- Never write code samples, curl commands, or JSON bodies.
- Never use the word "just" in "your admins just do X". Customers hate it.
- Don't list every supported data source. Microsoft + "plus Google,
  Snowflake, Salesforce, NetSuite, and more" is enough.
- Don't explain *why* OBO only works for Microsoft resources. The scope
  limit belongs in the IT appendix.

## What I want back in your response

1. The deck file itself (`.pptx`, `.html`, or `.md` per the preference
   order above), saved to `./sso-obo-customer-deck.<ext>`.
2. A **separate one-page speaker-notes appendix** in markdown
   (`./sso-obo-speaker-notes.md`) with one paragraph per slide, in plain
   English, to help the presenter narrate each slide.
3. A **technical appendix** in markdown (`./sso-obo-it-appendix.md`) that
   the presenter can send to the customer's IT team *after* the pitch.
   This is where all the acronyms live: OBO, auth-code+PKCE, exact scopes
   to grant (`https://analysis.windows.net/powerbi/api/.default offline_access`,
   `https://api.fabric.microsoft.com/.default offline_access`), redirect URIs,
   and the two redirect paths (`/api/auth/entra/callback` and
   `/api/connections/oauth/callback`).

Keep the deck to ~12 slides. If you find yourself exceeding 14, consolidate.
