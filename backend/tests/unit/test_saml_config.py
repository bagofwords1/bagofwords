import pytest
from pydantic import ValidationError
from app.settings.bow_config import SAMLProvider, BowConfig


def provider(**overrides):
    return dict(
        name="company-sso",
        enabled=True,
        organization_id="org",
        idp={"metadata_url": "https://idp.example.com/metadata"},
        **overrides,
    )


def test_saml_configuration_loads_without_affecting_existing_auth():
    cfg = BowConfig(base_url="https://localhost:3000", saml_providers=[provider()])
    assert cfg.saml_providers[0].attributes.subject == "name_id"
    assert not cfg.saml_providers[0].auto_provision_users
    assert BowConfig().saml_providers == []


@pytest.mark.parametrize(
    "url",
    ["http://localhost:3000", "https://user:pass@example.com", "https://example.com/path", "https://example.com?x=1"],
)
def test_enabled_saml_requires_canonical_https_origin(url):
    with pytest.raises(ValidationError):
        BowConfig(base_url=url, saml_providers=[provider()])


@pytest.mark.parametrize(
    "idp",
    [
        {},
        {"metadata_url": "http://idp.example.com/metadata"},
        {"metadata_file": "/etc/idp.xml", "metadata_url": "https://idp.example.com/metadata"},
        {"entity_id": "issuer", "sso_url": "https://example.com/sso"},
    ],
)
def test_incomplete_or_ambiguous_trust_configuration_rejected(idp):
    data = provider()
    data["idp"] = idp
    with pytest.raises(ValidationError):
        SAMLProvider(**data)


def test_duplicate_provider_names_rejected():
    with pytest.raises(ValidationError):
        BowConfig(base_url="https://localhost:3000", saml_providers=[provider(), provider()])


@pytest.mark.parametrize(
    "sp", [{"sign_requests": True}, {"want_assertions_encrypted": True}, {"private_key_file": "/tmp/key"}]
)
def test_sp_crypto_requires_key_pair(sp):
    with pytest.raises(ValidationError):
        SAMLProvider(**provider(sp=sp))
