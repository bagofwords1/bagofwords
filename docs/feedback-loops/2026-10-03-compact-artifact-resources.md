# Compact artifact Resources explorer

The Resources modal used oversized spacing, native selects and an always-visible filter form. The updated explorer uses a compact toolbar, searchable collection menu, consistent `USelectMenu` controls, optional schema/filter panels and denser table rows. It remains read-only.

Filtering separates draft input from the applied condition. Apply converts numeric and boolean values to their schema types; Refresh and pagination retain the applied condition. Clear removes it, and changing collections resets it. Enum and boolean fields use value menus. Record counts support singular/plural wording.

## Reproduce and verification

Open an artifact with a collection, then choose **Resources** from the dashboard menu. Expand Filter, choose an indexed field, enter/select a value and Apply. Change the draft value without applying, then Refresh: the original condition must remain active. Clear restores unfiltered results. Expand the schema, inspect a record, and change collections.

The local production-build browser fixture rendered the actual component against synthetic API responses with two collections and string/enum/boolean/number fields. A temporary copy of the original component supplied the same-viewport before screenshot. Neither fixture component nor route is retained in the source.

Observed checks:

- Browser: enum filtering, unapplied draft isolation on Refresh, Clear, boolean `false`, numeric `0`, pagination cursor and collection reset passed.
- Schema expansion passed; English mobile (390px) and Hebrew dark mode (1280px) had no document horizontal overflow.
- Vue script/template compilation and new-key parity across all ten locale catalogs passed.
- Production static generation passed, including the final build after fixture removal.
- No backend calls against real data, mutations or deployment were performed for this UI follow-up. Existing authorization and runtime endpoints are unchanged. This is basic UI verification, not full live integration QA.

## Evidence

- [Before](../../media/pr/artifact-resources/compact-before.png)
- [After](../../media/pr/artifact-resources/compact-after.png)
- [Applied filter](../../media/pr/artifact-resources/compact-filter.png)
- [Filter interaction](../../media/pr/artifact-resources/compact-filter-flow.gif)
- [Mobile](../../media/pr/artifact-resources/compact-mobile.png)
- [Hebrew / dark mode](../../media/pr/artifact-resources/compact-he.png)

Implementation: `frontend/components/ArtifactResourceExplorer.vue`; labels: `locales/*.json` under `artifactResources`.
