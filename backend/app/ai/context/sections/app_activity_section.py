from typing import Optional

from .base import ContextSection, xml_escape, xml_tag


class AppActivitySection(ContextSection):
    """What went wrong in this conversation's apps since the agent last
    looked: failures from the editor's own use (and BOW's preview), each with
    the few steps before it (services/artifact_activity.py). Absent when
    nothing failed, so it costs nothing on a normal turn."""

    tag_name = "app_activity"

    text: Optional[str] = None

    def render(self) -> str:
        if not self.text:
            return ""
        intro = ("Recorded while the user (or your preview) used the app since your last answer. "
                 "Lines starting with ! failed; vN is the app version that was running. "
                 "Use this before asking the user what went wrong; "
                 "read_artifact_activity gives more, or one event's detail.")
        return xml_tag(self.tag_name, xml_escape(intro + "\n" + self.text))
