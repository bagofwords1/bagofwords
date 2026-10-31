"""The artifact-chat context tells the agent where its reply is shown.

An app's conversation (bow.conversations) shows the reply and each
create_data result, often in another system and another language; the
dashboard bubble keeps its dashboard framing, its TEXT-ONLY surface and its
data-only scope.
"""
from app.ai.agents.planner.prompt_builder import PromptBuilder

fmt = PromptBuilder._format_artifact_chat_context


def test_an_app_conversation_is_told_to_answer_directly_in_the_persons_language():
    text = fmt({"surface": "app", "scope": "agents", "artifact_title": "עוזר מכירות"})
    assert "Reply in the language the person writes in" in text
    assert "do not narrate your plan" in text
    assert "App: " in text and "grounded in the dashboard" not in text
    assert "dashboard's own data ONLY" not in text
    assert "RESULTS SURFACE" in text and "TEXT-ONLY SURFACE" not in text


def test_the_dashboard_bubble_keeps_its_framing():
    text = fmt({"scope": "data_only", "artifact_title": "Sales", "visualizations": []})
    assert "VIEWER of a shared dashboard" in text and "Dashboard: " in text
    assert "dashboard's own data ONLY" in text
    assert "Reply in the language" not in text
    assert "TEXT-ONLY SURFACE" in text and "RESULTS SURFACE" not in text
