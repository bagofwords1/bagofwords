# Collaborative dashboards — manual QA test cases

Goal: sharing, editing versions, letting others edit, and publishing feel
simple. Versions, copies and re-runs stay invisible unless the user needs them.
Each case lists **steps → expected result**, plus a **UX check** (is it obvious
without explanation?). Run each case in `en` and spot-check it in `he` (RTL).

Personas: **Dana** (owner), **Avi** (editor), **Noa** (viewer), **Gil**
(member with no access), **anon** (no login). Data: Chinook sqlite, plus a
data source with row-level security (RLS) per user.

## A. Create and publish
| # | Steps | Expected | UX check |
|---|---|---|---|
| A1 | Dana asks the agent for a dashboard | v1 appears in the Dashboard tab. Toolbar shows "Publish v1" with an amber dot | One obvious next step: Publish |
| A2 | Click Publish v1 | Spinner, then "Live" with a check. `/d/{id}` shows v1 | No dialog for a first publish |
| A3 | Ask for a change | v2 is selected automatically. The button reads "Publish v2" | Dana never picks a version by hand |
| A4 | Publish v2 | Live becomes v2. Viewers see it after the re-run | No warning, since it's her own line |
| A5 | Reload in the middle of a run | When the run ends, the pane follows the new version | No "New artifact" pill for her own run |

## B. Versions
| # | Steps | Expected | UX check |
|---|---|---|---|
| B1 | Open the version picker | Versions newest first, with a Live pill and names | One list, no "drafts" or "branches" |
| B2 | Pick an older version, send an edit | The note above the prompt says "builds on v1 · v3 is newer" and offers Open v3 | The base version is never a surprise |
| B3 | Settings › Versions: name one | The name shows in the picker and the list | |
| B4 | Restore an old published version | It is live at once (no re-run). Toast "vN is live again" | One confirm, written in plain words |
| B5 | Publish this version (an unpublished one) | Its queries run, then it is live | |
| B6 | `?version=N` in the URL | Opens that version | |

## C. Share and access
| # | Steps | Expected | UX check |
|---|---|---|---|
| C1 | Share: add Avi as Can edit | Chip, then Share. Avi is listed. Avi gets an email if the toggle is on | Like Google Docs. No inner scrolling and no clipped menus |
| C2 | Avi shares onward | Allowed: editors can always share | |
| C3 | General access: Restricted → Organization → Anyone with link | Gil and anon gain or lose access to match | The descriptions say who can see it |
| C4 | Remove Avi | Avi's next request is refused, and the dashboard leaves Avi's list | |
| C5 | Noa (viewer) opens `/d/{id}` | Sees Live only, with no edit, publish or versions controls | |
| C6 | Gil opens the link while it's restricted | Access denied page with a clear message | |
| C7 | A project member, dashboard in a project | Access matches the project's role | |

## D. Others edit
| # | Steps | Expected | UX check |
|---|---|---|---|
| D1 | Avi opens the empty Dashboard tab and picks Dana's dashboard | It is copied in, charts run as Avi, and the agent can edit it | One click |
| D2 | Avi edits and publishes | Live = Avi's version. Dana sees "Avi published v5" | |
| D3 | Dana opens her conversation (no unpublished work) | Shows v5 read-only with "Edit v5 here". No new version is created just by opening | Nothing is copied just to look |
| D4 | Dana has unpublished work when live moves on | Banner: "Avi published v5 after your v3", with View v5 / Keep working on v3 | Her work is never buried |
| D5 | Dana publishes an older version over Avi's | The question names Avi and both versions. Open v5 / Publish v3 anyway | |
| D6 | Dana publishes an old version over her own newer live one | The question says "You published…" | Never "their changes" about yourself |
| D7 | Two editors at once | Presence avatars, and an outline while the agent edits | |
| D8 | Ask "what changed?" | The agent answers from history and does **not** edit the dashboard | |

## E. Data, RLS and identity
| # | Steps | Expected | UX check |
|---|---|---|---|
| E1 | RLS source: viewers each see only their own rows | Noa ≠ Avi in the numbers. Nobody sees the author's rows unless "run as author" is set | |
| E2 | Copying into another conversation | Queries run again as the person copying. Rows are never copied | |
| E3 | A public dashboard with monitoring data | Refused with a clear message | |
| E4 | Refresh schedule in Settings › General | Saved. The next run follows it | |
| E5 | Data tab, chat, and run identity in Settings | The viewer page follows each setting | |

## F. Settings window
F1: the tabs (General, Viewers, Notes, Data, Resources, Analytics, History) each load. F2: rename and the link slug save with one Save. F3: transfer ownership. F4: works at phone width and in RTL.

## G. Robustness (API)
G1: an editor's capabilities match the UI. G2: a viewer calling publish, bring or rename gets 403/404. G3: a double-click on Publish starts one publish only. G4: a bring for another user's conversation returns 404. G5: deleting a conversation keeps the dashboard while others still edit it.
