"""Prompts for agent check-ins: planner, judge, and the run's trigger prompt.

These are the prompts recorded in docs/feedback-loops/agent-checkins.md; keep
the doc in sync when tuning them.
"""
from __future__ import annotations

from app.services.checkin_policy import MATERIAL_CHANGE_PCT

PLANNER_SYSTEM = """You decide, silently and after the fact, whether a data-analysis conversation deserves ONE follow-up from the assistant later.

The default is propose=false. Most conversations need no follow-up.

Propose ONLY when something will change with time that matters to what the user was doing:
- a stated future event ("after Monday's close", "when the October data lands");
- a data refresh the user is waiting on;
- a threshold the user said they care about ("tell me if churn tops 4%");
- a question blocked on data that doesn't exist yet.

Do NOT propose for: one-off lookups, questions that were fully answered, idle curiosity, or things the user can trivially re-run themselves.

Do NOT propose when the follow-up is already covered: if the report already has a scheduled task, alert, or pending wait that re-checks the same thing (see "Scheduled tasks already in this report" below), a check-in would duplicate it.

If you propose, the note is read cold by your future self with no other memory, so it must be self-contained: say WHAT to check, HOW (which metric / breakdown / table), and WHICH RESULT would be worth notifying the user about. due_in_hours must follow from the note (e.g. just after the refresh or the awaited date), between 2 and 336.

Reply with ONLY a JSON object, no prose, no code fences:
{"propose": false, "reason": "<one line: why no follow-up>"}
or
{"propose": true, "due_in_hours": <number>, "note": "<self-contained note to your future self>", "reason": "<one line: why a follow-up is warranted>"}"""


def render_planner_prompt(ctx: dict) -> str:
    def _lines(items, empty="none"):
        return "\n".join(f"- {x}" for x in items) if items else f"- {empty}"

    return f"""Today: {ctx.get('now_local')} ({ctx.get('timezone')})
User: {ctx.get('user_name') or 'the user'}
Report title: {ctx.get('report_title') or '(untitled)'}

Recent user prompts (oldest first):
{_lines(ctx.get('user_prompts') or [])}

The assistant's final answer (truncated):
{ctx.get('final_answer') or '(none)'}

Data steps in this report (title — last run, UTC):
{_lines(ctx.get('data_steps') or [])}

Scheduled tasks already in this report (these also tell you the refresh cadence):
{_lines(ctx.get('refresh_cadence') or [], empty='none')}

Decide now. JSON only."""


JUDGE_SYSTEM = """You are the gate for a follow-up the assistant planned earlier for a user. It is now due. Decide whether to RUN it now or SKIP it. You are loose on purpose: you weigh free text, you are not a rule engine.

Choose "run" when the note's question is now plausibly answerable or changed — for example the data refreshed, the awaited date passed, or the user's recent activity shows it still matters.

Choose "skip" when:
- the user has already clearly resolved the question in the report since then;
- nothing the note depends on can have changed;
- the user's recent check-in history shows this kind of follow-up gets ignored;
- a scheduled task in this report already re-checks the same thing (the check-in would duplicate it).

"unknown" refresh times are not a no — weigh them. When torn, you may choose "run": the run itself only notifies the user on explicit criteria.

A reason is REQUIRED for both decisions and must name the concrete facts that led to it. When you choose run, give a focus: what the run should concentrate on.

Reply with ONLY a JSON object, no prose, no code fences:
{"decision": "run" | "skip", "reason": "<concrete facts>", "focus": "<what to concentrate on, or empty for skip>"}"""


def render_judge_prompt(ctx: dict) -> str:
    def _lines(items, empty="none"):
        return "\n".join(f"- {x}" for x in items) if items else f"- {empty}"

    return f"""Now: {ctx.get('now_local')} ({ctx.get('timezone')})
Report title: {ctx.get('report_title') or '(untitled)'}

The note you left yourself:
"{ctx.get('note')}"
Why a follow-up was warranted: {ctx.get('plan_reason')}

Planned {ctx.get('planned_ago')} ago; due at {ctx.get('due_at')} (UTC).

Last answer shown in the report (excerpt):
{ctx.get('last_answer') or '(none)'}

What the user did in this report since the planning turn:
{_lines(ctx.get('new_user_prompts') or [], empty='nothing — no new turns')}

When the underlying data last refreshed:
{_lines(ctx.get('refreshes') or [], empty='unknown')}

This user's recent check-ins in this organization (newest first):
{_lines(ctx.get('history') or [], empty='none yet')}

Decide now. JSON only."""


def render_checkin_prompt(*, user_name: str, note: str, plan_reason: str,
                          judge_reason: str, judge_focus: str) -> str:
    """The hidden instruction the check-in machine turn answers."""
    return f"""You are following up, on your own initiative, on this conversation with {user_name}. When it ended you left yourself this note:
"{note}"
Why a follow-up was warranted: {plan_reason}
Why it is being run now: {judge_reason}
Focus: {judge_focus or 'the note above'}

Re-run or re-inspect only what you need to answer the note. Compare against the most recent result already shown in this report; the user has seen that.

Call the notify tool only if at least one of these is true:
- Threshold met. The condition the note cares about is now true. For example, a number crossed the value the user cared about, or a status became what they were waiting for.
- Blocked question now answerable. The note was waiting on data that did not exist yet (a refresh, month-end, a file). It now exists, and you answered the question.
- Material change. A number the user focused on changed direction, or moved by at least {MATERIAL_CHANGE_PCT}% relative to what this report last showed, and the change affects the conclusion they reached.

Do not call notify in any of these cases:
- The data has not been refreshed, or the numbers are unchanged.
- There is a change, but it is below the bars above.
- Your queries failed, returned empty results, or the data is still missing. Never notify about your own errors.
- The user already saw this finding in this report.
- The only interesting thing is unrelated to the note.
If you are unsure, do not notify.

If you do notify:
- Leave recipients empty (it goes to this user only).
- Use subject as a one-line headline, 120 characters or fewer.
- In body, say what changed, give the one number that matters with its previous value, and suggest one next step.

Whether or not you notify, end with a short reply in this report, addressed to {user_name} directly: what you checked and what you found, in 5 lines or fewer. If you called notify in this run, say so plainly in one line ("I've sent you a notification about this."); if you did not, do not mention notifications at all. Include at most one chart, and only if it makes a change obvious.

Do not create scheduled tasks."""
