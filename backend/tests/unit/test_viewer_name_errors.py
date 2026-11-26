"""The signed-in viewer's name never goes in a page's title or headings.

On the benchmark, a dashboard joined ``useCurrentUser().name`` to its title, so
everyone who opened the shared page saw one person's name as its title. A
prompt rule alone did not stop it.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai.tools.implementations._artifact_refs import viewer_name_errors  # noqa: E402

V11 = {"runtime": {"version": 11}}


def page(body, hook="const u = useCurrentUser();"):
    return f"function App() {{ {hook} return (<div>{body}<TrendChart viz={{v}} x=\"m\" y=\"r\" /></div>); }}"


def test_the_viewers_name_in_a_title_or_subtitle_is_flagged():
    flagged = [
        page("<PageHeader title={`${u?.name ? u.name + ' · ' : ''}Sales pipeline`} />"),
        page("<PageHeader title=\"Sales\" subtitle={`${u?.name ? u.name + ' · ' : ''}data for ${month}`} />"),
        page("<SectionCard eyebrow={u.email} title=\"Revenue\">x</SectionCard>"),
        page("<h1>{name}'s dashboard</h1>", hook="const { name } = useCurrentUser();"),
        page("<PageHeader title={useCurrentUser()?.name} />", hook=""),
    ]
    for code in flagged:
        errs = viewer_name_errors(code, V11)
        assert len(errs) == 1 and errs[0].startswith("[viewer]"), code


def test_greetings_signed_in_lines_and_other_people_are_allowed():
    allowed = [
        page("<PageHeader title=\"Discounts\" subtitle={u?.name ? `Welcome back, ${u.name}` : 'Welcome back'} />"),
        page("<PageHeader title=\"Discount request\" subtitle={u && u.name ? 'Signed in as ' + u.name : 'Request a discount'} />"),
        page("<p>Prepared for {u?.name}</p>"),                                  # body text, not a title
        page("<PageHeader title={rep.name} subtitle=\"Rep scorecard\" />"),     # a person from the data
        page("<PageHeader title=\"Sales\" />", hook=""),                        # no current user at all
        page("<PageHeader title={u?.groups?.[0] || 'Team'} />"),                # a group, not a name
    ]
    for code in allowed:
        assert viewer_name_errors(code, V11) == [], code


def test_an_edit_answers_only_for_a_name_it_adds():
    before = page("<PageHeader title={u.name} />")
    unrelated = before.replace("<TrendChart", "<p>Updated</p><TrendChart")
    assert viewer_name_errors(unrelated, V11, previous_code=before) == []
    clean = page("<PageHeader title=\"Sales\" />")
    assert len(viewer_name_errors(before, V11, previous_code=clean)) == 1
