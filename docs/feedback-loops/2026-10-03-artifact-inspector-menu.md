# Separate Resources and Analytics entry points

The artifact More menu previously opened a combined Data & analytics dialog. It now provides Resources and Analytics as separate actions, each opening a focused dialog with its own title and no tab switcher.

The existing `ArtifactResourceExplorer` component accepts a view with Resources as the default, preserving the resource tool card's existing opener. Analytics loads only analytics; Resources loads collection definitions and records. Backend access rules and read-only behavior are unchanged. All ten locale labels were updated without changing catalog shape.

Verification uses a temporary local page rendering the real ArtifactFrame and explorer against synthetic API responses. Check both More-menu actions, their dialog titles, and captured requests: Resources must not load analytics; Analytics must not load definitions or records. Remove the temporary route before final verification. Screenshots are recorded under `media/pr/artifact-resources/`; the prior combined-dialog evidence is `analytics-after.png`.

Observed: both real menu actions opened the expected titled dialog; Resources displayed the synthetic task and issued no analytics request; Analytics displayed 12 synthetic views and issued no resource-definition or record request. Vue script/template compilation and locale-only diff checks passed. The temporary fixture and local development alias were removed. Deployed to bow on 2026-10-03 together with the read-artifact photo icon.

Final production static generation passed after removing the temporary route. No backend or storage changes were required.

Deployment verified all 2,126 static files and 88 served entry assets against the build. Health and sign-in returned HTTP 200; container healthy. Frontend-only swap required no restart or image change. Rollback: `/home/ubuntu/artifact-menu-backup-20261003T141304Z/rollback.sh`. Container recreation still requires an updated image or reapplying the frontend bundle.
