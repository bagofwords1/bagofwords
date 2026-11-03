import copy
import pytest
from app.ai.response_language import resolve_response_locale, localize_column_headers

@pytest.mark.asyncio
@pytest.mark.parametrize('message,selected,previous', [
    ('הצג מכירות לפי שנה', 'he', 'en'),
    ('Muestra las ventas por región', 'es', 'he'),
    ('ענה באנגלית בבקשה', 'en', 'he'),
    ('yes', 'es', 'es'),
    ('Bitte zeige die Umsätze', 'de', 'es'),
])
async def test_response_language_accepts_supported_classifier_result(message, selected, previous):
    async def model(prompt):
        return selected
    assert await resolve_response_locale(model, message, previous) == selected

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
