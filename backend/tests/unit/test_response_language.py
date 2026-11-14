import copy
import json
import pytest
from app.ai.response_language import resolve_response_locale, localize_column_headers

@pytest.mark.asyncio
@pytest.mark.parametrize('message,selected,previous', [
    ('הצג מכירות לפי שנה', 'he', 'en'),
    ('Muestra las ventas por región', 'es', 'he'),
    ('yes', 'es', 'es'),
    ('Bitte zeige die Umsätze', 'de', 'es'),
])
@pytest.mark.parametrize('shape', ['bare', 'json'])
async def test_response_language_accepts_supported_classifier_result(message, selected, previous, shape):
    async def model(prompt):
        return selected if shape == 'bare' else json.dumps({'language': selected, 'explicit': False})
    assert await resolve_response_locale(model, message, previous) == selected


@pytest.mark.asyncio
@pytest.mark.parametrize('message,requested', [
    ('ענה באנגלית בבקשה', 'en'),
    ('תסכם את ההכנסות לפי אזור, אבל תענה בצרפתית', 'fr'),
    ('اعرض المبيعات حسب المنطقة باللغة الإنجليزية من فضلك', 'en'),
])
async def test_explicit_request_for_another_language_wins_over_the_script(message, requested):
    async def model(prompt):
        return json.dumps({'language': requested, 'explicit': True})
    assert await resolve_response_locale(model, message, 'he') == requested


# Hebrew/Arabic questions that embed English identifiers — the everyday shape
# of an analytics question. Their letters are mostly Latin; their words are not.
MIXED_RTL_QUESTIONS = [
    ('תבנה לי dashboard של sales by region', 'he'),
    ('תראה לי revenue לפי customer_segment ב-Q3', 'he'),
    ('מה ה-MRR של Acme Corp בחודש האחרון?', 'he'),
    ('@Sales תשווה את ה-churn_rate בין enterprise ל-SMB', 'he'),
    ('اعرض revenue حسب customer_segment للربع الأخير', 'ar'),
]


@pytest.mark.asyncio
@pytest.mark.parametrize('message,script', MIXED_RTL_QUESTIONS)
@pytest.mark.parametrize('reply', ['en', '{"language": "en", "explicit": false}', 'de'])
async def test_classifier_slip_cannot_take_an_rtl_question_out_of_its_script(message, script, reply):
    async def model(prompt):
        return reply
    assert await resolve_response_locale(model, message, 'en', 'en') == script


@pytest.mark.asyncio
@pytest.mark.parametrize('message,script', MIXED_RTL_QUESTIONS)
@pytest.mark.parametrize('failure', ['raise', 'garbage', 'timeout_like_empty'])
async def test_without_a_classification_the_script_decides(message, script, failure):
    async def model(prompt):
        if failure == 'raise':
            raise RuntimeError('provider unavailable')
        return 'Hebrew' if failure == 'garbage' else ''
    assert await resolve_response_locale(model, message, 'en', 'en') == script


@pytest.mark.asyncio
@pytest.mark.parametrize('message', ['כן', 'תודה רבה'])
async def test_short_rtl_acknowledgment_may_inherit_the_conversation(message):
    async def model(prompt):
        return 'es'
    assert await resolve_response_locale(model, message, 'es') == 'es'


@pytest.mark.asyncio
@pytest.mark.parametrize('message', [
    'Show the revenue of חברת אלפא by month for the last two years',
    'Which customers churned last quarter and why? `SELECT * FROM שלום`',
    'Summarize https://example.com/דוח-מכירות for me please',
])
async def test_ltr_question_with_rtl_fragments_keeps_the_classifier_answer(message):
    async def model(prompt):
        return 'en'
    assert await resolve_response_locale(model, message, 'he') == 'en'

@pytest.mark.asyncio
@pytest.mark.parametrize('reply', ['xx', 'English please', '{"locale":"es"}', ''])
async def test_invalid_classifier_output_retains_prior_language(reply):
    async def model(prompt):
        return reply
    assert await resolve_response_locale(model, 'ok', 'fr', 'en') == 'fr'

@pytest.mark.asyncio
async def test_provider_failure_does_not_break_the_response():
    async def model(prompt):
        raise RuntimeError('provider unavailable')
    assert await resolve_response_locale(model, 'continue', 'es') == 'es'
    assert await resolve_response_locale(model, 'הצג מכירות לפי אזור', 'en') == 'he'

@pytest.mark.asyncio
async def test_column_labels_preserve_data_and_only_update_known_display_names():
    data = {'columns': [{'field': 'sales_2024', 'headerName': 'sales_2024', 'type': 'number'},
                        {'field': 'genre', 'headerName': 'genre'}],
            'rows': [{'sales_2024': 42, 'genre': 'Rock'}]}
    original = copy.deepcopy(data)
    async def model(prompt):
        return '{"sales_2024":"מכירות 2024", "genre":"ז׳אנר", "invented":"ignored"}'
    await localize_column_headers(model, data, 'he')
    assert data['rows'] == original['rows']
    assert [c['field'] for c in data['columns']] == [c['field'] for c in original['columns']]
    assert data['columns'][0]['type'] == 'number'
    assert data['columns'][0]['headerName'] != original['columns'][0]['headerName']
    assert len(data['columns']) == len(original['columns'])

@pytest.mark.asyncio
@pytest.mark.parametrize('reply', ['[]', 'invalid', '{"total":null}', '{"total":""}'])
async def test_invalid_column_labels_leave_original_metadata(reply):
    data = {'columns': [{'field': 'total', 'headerName': 'Total'}], 'rows': []}
    original = copy.deepcopy(data)
    async def model(prompt):
        return reply
    await localize_column_headers(model, data, 'es')
    assert data == original

@pytest.mark.asyncio
@pytest.mark.parametrize('locale', ['he', 'ar', 'es'])
@pytest.mark.parametrize('trigger', ['sub_agent_wave', 'wait', 'checkin'])
async def test_automatic_turns_inherit_language_without_classifying_internal_text(locale, trigger):
    calls = []
    async def model(prompt):
        calls.append(prompt)
        return 'en'
    assert await resolve_response_locale(
        model, '[Automatic continuation] Summarize the English records.', locale,
        trigger_source=trigger,
    ) == locale
    assert calls == []

@pytest.mark.asyncio
async def test_delayed_followup_uses_origin_snapshot_not_newer_conversation_language():
    async def model(prompt):
        return 'en'
    assert await resolve_response_locale(model, 'Synthesize results', 'en',
        trigger_source='sub_agent_wave', inherited='he') == 'he'

@pytest.mark.asyncio
async def test_legacy_followup_classifies_original_request_not_machine_instruction():
    async def model(prompt):
        import json
        data = json.loads(prompt.split('\n', 1)[1])
        assert data['user_request'] == 'اعرض المبيعات'
        return 'ar'
    assert await resolve_response_locale(model, 'Synthesize results in English', None,
        trigger_source='sub_agent_wave', original_request='اعرض المبيعات') == 'ar'


@pytest.mark.asyncio
async def test_real_user_turn_can_change_language_despite_inherited_hint():
    async def model(prompt):
        return 'en'
    assert await resolve_response_locale(model, 'Please answer in English', 'he',
                                         inherited='he') == 'en'


def test_persisted_machine_language_survives_response_schema_serialization():
    from app.schemas.completion_schema import PromptSchema
    from app.services.response_language_context import completion_response_metadata
    for locale in ['he', 'ar', 'fr']:
        saved = completion_response_metadata({'response_locale': locale}, 'wait')
        assert PromptSchema(**saved).model_dump()['response_locale'] == locale
        assert completion_response_metadata({'response_locale': locale}, None) is None
    assert completion_response_metadata({'response_locale': 'invalid'}, 'wait') is None
