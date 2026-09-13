# Knowledge Explorer sign-in spinner

## Cause and reproduction

`KnowledgeExplorer.vue` set `connectingAgentId` while authorizing OAuth, but its agent row never passed that state to the inline `TreeGroup` badge. The baseline browser capture showed no busy attribute and zero spinner animations during a held authorization request.

## Change

Pass the clicked agent's loading state into the badge, replace the key icon with the existing `Spinner.vue`, and disable the button while busy. A handler guard prevents overlapping sign-in starts. Error and credentials-modal paths clear the state in `finally`; successful redirects retain it until navigation leaves the page. Existing labels and button dimensions are preserved.

## Verification

With the local frontend on port 3100, run from `frontend`:

```sh
node tests/instructions/knowledge-signin-spinner.mjs
```

The real Knowledge Explorer renders two synthetic agents and uses intercepted API responses. Browser checks passed for clicked-row-only animation, duplicate-click protection, error cleanup, busy state at OAuth page exit, and Hebrew RTL. No live authentication or saved data is involved. The temporary preview route is removed automatically.

- [Before](../../media/pr/knowledge-signin-spinner/before.png)
- [After](../../media/pr/knowledge-signin-spinner/after.png)
- [Hebrew](../../media/pr/knowledge-signin-spinner/he.png)
- [Flow](../../media/pr/knowledge-signin-spinner/flow.gif)
