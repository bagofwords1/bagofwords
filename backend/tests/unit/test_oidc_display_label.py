"""OIDCProvider.display_label() feeds the "Sign in with {label}" button.

The label must therefore be a bare product name. Older k8s docs told
operators to set the whole sentence, which rendered as
"Sign in with Sign in with Microsoft"; a verb prefix in an explicit label is
stripped so both forms of config produce one sentence.
"""
import pytest

from app.settings.bow_config import OIDCProvider


def _provider(**overrides) -> OIDCProvider:
    base = dict(name="entra", issuer="https://login.microsoftonline.com/tenant/v2.0")
    base.update(overrides)
    return OIDCProvider(**base)


@pytest.mark.parametrize(
    "label",
    [
        "Sign in with Microsoft",
        "sign in with Microsoft",
        "Signin with Microsoft",
        "Log in with Microsoft",
        "Login with Microsoft",
        "Continue with Microsoft",
        "  Sign in with  Microsoft  ",
    ],
)
def test_explicit_label_drops_sign_in_verb(label):
    assert _provider(label=label).display_label() == "Microsoft"


def test_explicit_bare_label_is_kept_verbatim():
    assert _provider(label="Contoso SSO").display_label() == "Contoso SSO"


def test_label_that_is_only_the_verb_falls_back_to_itself():
    # Nothing sensible to strip to; don't return an empty button.
    assert _provider(label="Sign in with").display_label() == "Sign in with"


def test_label_containing_with_mid_sentence_is_untouched():
    assert _provider(label="Login with SSO Portal", name="portal").display_label() == "SSO Portal"
    assert _provider(label="Partners with Benefits").display_label() == "Partners with Benefits"


def test_no_label_uses_brand_then_slug():
    assert _provider().display_label() == "Microsoft"
    assert _provider(name="corp-sso", issuer="https://sso.corp.example/oauth2").display_label() == "Corp Sso"
