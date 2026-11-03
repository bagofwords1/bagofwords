"""Machine continuations preserve user language through real completion creation."""
import json
import pytest
from sqlalchemy import select
from app.dependencies import async_session_maker
from app.models.completion import Completion
from app.models.report import Report
from app.models.user import User
from app.models.organization import Organization
from app.services.machine_turn import run_machine_turn
from app.services.response_language_context import machine_response_context
from tests.e2e.test_sub_agents import _env, _run, scripted_llm, live_client


@pytest.fixture
def reply_births():
    # Observe persisted initial state without replacing any application service.
    from sqlalchemy import event
    records = []
    def capture(mapper, connection, row):
        if row.role == 'system' and row.trigger_source == 'sub_agent_wave':
            records.append(dict(row.prompt or {}))
    event.listen(Completion, 'after_insert', capture)
    try:
        yield records
    finally:
        event.remove(Completion, 'after_insert', capture)


@pytest.mark.parametrize('locale,user_text', [('he', 'הצג את המכירות'), ('ar', 'اعرض المبيعات')])
@pytest.mark.parametrize('legacy', [False, True])
def test_delayed_machine_reply_preserves_origin_language(
    live_client, create_user, login_user, whoami, create_report,
    create_llm_provider_and_models, monkeypatch, scripted_llm, reply_births, locale, user_text, legacy,
):
    scripted_llm()
    from app.ai.llm.llm import LLM
    classified = []
    async def language_boundary(self, prompt, **kwargs):
        if kwargs.get('usage_scope') == 'response.language':
            data = json.loads(prompt.split('\n', 1)[1])
            classified.append(data['user_request'])
            yield locale if data['user_request'] == user_text else 'en'
        else:
            yield 'Summary'
    monkeypatch.setattr(LLM, 'inference_stream', language_boundary)
    report, headers, org_id, token = _env(create_user, login_user, whoami, create_report,
                                         create_llm_provider_and_models, monkeypatch)

    async def scenario():
        async with async_session_maker() as db:
            r = await db.get(Report, report['id'])
            org = await db.get(Organization, org_id)
            user = await db.get(User, r.user_id)
            # Historic/legacy metadata and a delayed result after a newer turn
            # cannot be produced through a single completion API user_text.
            head = Completion(report_id=r.id, user_id=user.id, role='user', turn_index=0,
                              prompt={'content': user_text}, completion={}, status='success')
            db.add(head)
            await db.flush()
            parent = Completion(report_id=r.id, user_id=user.id, role='system', turn_index=1,
                                parent_id=head.id, prompt={} if legacy else {'response_locale': locale},
                                completion={}, status='success')
            newer = Completion(report_id=r.id, user_id=user.id, role='system', turn_index=3,
                               prompt={'response_locale': 'en'}, completion={}, status='success')
            db.add_all([parent, newer])
            await db.commit()
            context = await machine_response_context(db, r.id, parent_completion_id=parent.id)
            if not legacy:
                assert context['response_locale'] == locale
            else:
                assert context['response_original_request'] == user_text
            # Exercise the real machine-turn -> schema -> completion -> agent path.
            event = await run_machine_turn(db, report=r, user=user, organization=org,
                summary='Background work finished', trigger_source='sub_agent_wave',
                message_type='sub_agent_wave_done',
                instruction='<sub_agent_results>English research records</sub_agent_results>',
                parent_completion_id=parent.id)
            assert event.prompt['response_locale'] == locale
            replies = (await db.execute(select(Completion).where(
                Completion.report_id == r.id, Completion.role == 'system',
                Completion.trigger_source == 'sub_agent_wave',
            ))).scalars().all()
            assert len(replies) == 1
            assert replies[0].status == 'success'
            assert replies[0].prompt['response_locale'] == locale
    _run(scenario())
    assert classified == ([user_text] if legacy else [])
    assert len(reply_births) == 1
    assert reply_births[0]['response_locale'] == locale
