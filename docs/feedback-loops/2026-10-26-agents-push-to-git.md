# Feedback Loop — "so we don't have any push?" (Agents page has no Push to Git)

Admins with a connected, write-enabled git repo had no way to push instruction
changes to git from the Agents page. "Push to Git" (branch `BOW-<build>` plus a
PR when a token is set) existed only in the Version Explorer, which the Agents
page never opened with a repo id. This loop shows the button missing, then
working end to end against a real git remote.

## Root cause (validated)

- The push UI is in `frontend/components/instructions/BuildExplorerModal.vue`.
  It renders only when the parent passes `gitRepoId`.
- The only parent that passes it is `frontend/components/ConsoleInstructions.vue`,
  which now lives at the unlinked `/settings/instructions` route. The Agents
  page (`frontend/components/KnowledgeExplorer.vue`) mounts the git
  *connection* button and modal, but never a builds view with a repo id.
- Backend `POST /api/git/{id}/push` (`backend/app/routes/git.py:308`) was
  unaffected. It requires org-level `manage_instructions`.
- Also found: inside the Version Explorer, the push button was gated on
  `useCanAny('manage_instructions', 'data_source')`. Per-agent managers saw it
  and then got a 403. It is now gated on org-level `manage_instructions`.

## Loop — live, self-contained (no external services)

The git remote is a local bare repo served through `git http-backend` over TLS.
BOW only accepts a PAT over `https://`, so the backend is started with
`GIT_SSL_CAINFO` pointing at the self-signed cert. No code changes are needed.

```bash
L=docs/feedback-loops/2026-10-26-agents-push-to-git
W=/tmp/pushloop && mkdir -p $W/srv/acme && cd $W
git init -q --bare -b main srv/acme/instructions.git
git -C srv/acme/instructions.git config http.receivepack true
git init -q -b main seed && (cd seed && echo hi > README.md && git add . \
  && git -c user.email=a@b -c user.name=s commit -qm init && git push -q ../srv/acme/instructions.git main)
openssl req -x509 -newkey rsa:2048 -nodes -keyout k.pem -out c.pem -days 2 \
  -subj /CN=127.0.0.1 -addext "subjectAltName=IP:127.0.0.1"
setsid python3 ~/bagofwords/$L.git_server.py $W/srv 9419 $W/c.pem $W/k.pem &

cd ~/bagofwords
tools/agent/boot_stack.sh --dev
(cd backend && uv run python ../tools/agent/seed_org.py --demo --invite member@example.com)
GIT_SSL_CAINFO=$W/c.pem tools/agent/restart_backend.sh $PWD/backend/db/agent.db
(cd backend && uv run python ../$L.setup.py connect)      # repo: can_push True
cd frontend
node ../$L.verify.mjs before   $W   # run with the change stashed
node ../$L.verify.mjs after    $W
node ../$L.verify.mjs member   $W
node ../$L.verify.mjs he       $W
(cd ../backend && uv run python ../$L.setup.py write off)
node ../$L.verify.mjs disabled $W
(cd ../backend && uv run python ../$L.setup.py member-push)
git -C $W/srv/acme/instructions.git branch    # BOW-<n> present
```

### Observed

| Leg | Output |
|---|---|
| before (base code, admin) | `[before] push button count: 0` |
| after (admin) | `push button count: 1` → Version Explorer opens with `Push to Git` visible → `POST /api/git/464524a6…/push {"build_id":"6c5a…","create_pr":true}` → `200 {"branch_name":"BOW-12","pushed":true,"pr_url":null}` → toast "Pushed to Git · Created branch BOW-12" |
| remote | `git branch` → `BOW-12`, `main`. Commit `BOW #12: Added 1 instruction`, which contains `bow/general/fiscal-calendar.md` |
| member (no `manage_instructions`) | `[member] push button count: 0`; the API push returns `403 Permission denied: this action needs 'manage_instructions'` |
| write access off | `isDisabled: true`, title "Turn on write access in the repository settings to push" |
| `he` locale | Button renders as "דחיפה ל-Git" in the mirrored header |

`pr_url` is null because the loop uses provider `custom`, which has no PR API.
PR creation for GitHub/GitLab/Bitbucket is existing, unchanged code.

## The fix

- `KnowledgeExplorer.vue`: a "Push to Git" button next to the git connection
  button. It shows only with `manage_instructions` (same gate as the git button
  and the API) and only when a repo is connected. It is disabled with a hint
  until a repo has `can_push`. It opens `BuildExplorerModal` with that repo's id.
- `BuildExplorerModal.vue`: the push button is gated on
  `useCan('manage_instructions')`.
- `locales/{en,es,he}.json`: `agentsPage.pushToGit`, `pushToGitTip`, and
  `pushToGitDisabledTip`.

## Regression notes

- `uv run pytest tests/e2e/rbac/test_rbac_registry.py -k frontend` → 1 passed
  (each `useCan` literal is a real permission).
- Locale sync: the new keys are present in all three catalogs. The catalogs
  were already out of sync on base, with `onboarding.llm.vertex*` missing from
  es/he. This change does not touch that.
