"""Plain-text copy for transactional emails.

Kept as small pure functions returning (subject, body) so the wording lives in
one place, reads like a person wrote it, and is trivial to test/preview. Bodies
are plain text on purpose -- no HTML, no buttons; links go inline on their own
line so they stay clickable in any client.
"""

from typing import List, Optional, Tuple

SIGNATURE = "BOW"


def invite_email(sign_up_url: str) -> Tuple[str, str]:
    subject = "You've been invited to BOW"
    body = (
        "Hi,\n\n"
        "Your team is using BOW to explore their data and answer questions in "
        "plain language, and they've invited you to join them.\n\n"
        "You can set up your account here:\n"
        f"{sign_up_url}\n\n"
        "If this doesn't look like something you were expecting, feel free to "
        "ignore this email.\n\n"
        f"{SIGNATURE}"
    )
    return subject, body


def welcome_email(name: Optional[str], agent_names: List[str], app_url: str) -> Tuple[str, str]:
    subject = "Welcome to BOW"
    greeting = f"Hi {name}," if name else "Hi,"

    if agent_names:
        shown = agent_names[:5]
        lines = "\n".join(f"  - {n}" for n in shown)
        if len(agent_names) > len(shown):
            lines += f"\n  - …and {len(agent_names) - len(shown)} more"
        agents_block = (
            "You've already got access to a few agents to get you started:\n"
            f"{lines}"
        )
    else:
        agents_block = (
            "Your team hasn't connected any agents yet. As soon as they do, "
            "you'll be able to start chatting with them."
        )

    body = (
        f"{greeting}\n\n"
        "Welcome to BOW! It's where you can ask questions about your data, run "
        "analyses and build dashboards or automations -- just by typing what you "
        "want in plain language.\n\n"
        f"{agents_block}\n\n"
        "Whenever you're ready, jump in here:\n"
        f"{app_url}\n\n"
        "Glad to have you on board,\n"
        f"{SIGNATURE}"
    )
    return subject, body
