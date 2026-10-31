import pytest
from app.services import mcp_cooldown as cooldown

@pytest.mark.parametrize('error,expected', [('API rate limit: retry after 29 seconds',29), ('429 Too Many Requests',60), ('Rate limit: try again in 2 minutes',120), ('Invalid argument',None)])
def test_provider_backoff(error,expected):
    assert cooldown.retry_after(error) == expected


def test_cooldown_is_principal_scoped_and_expires(monkeypatch):
    clock=[100.0]
    monkeypatch.setattr(cooldown.time,'monotonic',lambda:clock[0])
    key=('org','viewer','source')
    cooldown.record(key,'Rate limit retry after 30 seconds')
    assert cooldown.remaining(key)==30
    assert cooldown.remaining(('org','other','source'))==0
    clock[0]=131
    assert cooldown.remaining(key)==0
