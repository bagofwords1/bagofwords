# Pre-release patches

Integration branch collecting security patches for the next release.
Child PRs target this branch; this branch merges into `main` once they land.

| PR | Change |
|---|---|
| #1095 | security(code-exec): run generated code in a sandboxed child process |
| #1303 | fix(auth): stop reflecting OAuth/query error text into sign-in/sign-up (XSS) |
