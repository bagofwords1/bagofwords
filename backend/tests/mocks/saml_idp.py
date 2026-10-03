"""Independent, local SAML IdP boundary for deterministic interoperability tests.

Produces standards-shaped XML with real temporary RSA signatures. No BOW
validation or identity service is mocked. Also used by the browser sandbox.
"""

import base64
import uuid
from datetime import datetime, timedelta, timezone
from lxml import etree as E
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import hashes, serialization
from onelogin.saml2.utils import OneLogin_Saml2_Utils

A = "urn:oasis:names:tc:SAML:2.0:assertion"
P = "urn:oasis:names:tc:SAML:2.0:protocol"


def key_pair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "BOW sandbox IdP")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=2))
        .sign(key, hashes.SHA256())
    )
    return (
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode(),
        cert.public_bytes(serialization.Encoding.PEM).decode(),
    )


def response_xml(
    request_id,
    acs,
    audience,
    issuer,
    key,
    cert,
    *,
    subject="subject-123",
    email="person@example.com",
    attributes=None,
    variant="valid",
    signature="assertion",
):
    now = datetime.now(timezone.utc)
    stamp = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
    until = now + timedelta(minutes=4)
    if variant == "expired":
        until = now - timedelta(minutes=10)
    if variant == "wrong_request":
        request_id = "_another-request"
    if variant == "wrong_audience":
        audience += "/other"
    if variant == "wrong_issuer":
        issuer += "/other"
    if variant == "wrong_destination":
        acs += "/other"
    attrs = attributes if attributes is not None else {"mail": email, "display": "Sandbox Person"}
    response = E.Element(
        E.QName(P, "Response"),
        nsmap={"samlp": P, "saml": A},
        ID="_" + uuid.uuid4().hex,
        Version="2.0",
        IssueInstant=stamp(now),
        Destination=acs,
        InResponseTo=request_id,
    )
    E.SubElement(response, E.QName(A, "Issuer")).text = issuer
    status = E.SubElement(response, E.QName(P, "Status"))
    E.SubElement(status, E.QName(P, "StatusCode"), Value="urn:oasis:names:tc:SAML:2.0:status:Success")
    assertion = E.SubElement(
        response, E.QName(A, "Assertion"), ID="_" + uuid.uuid4().hex, Version="2.0", IssueInstant=stamp(now)
    )
    E.SubElement(assertion, E.QName(A, "Issuer")).text = issuer
    sub = E.SubElement(assertion, E.QName(A, "Subject"))
    E.SubElement(
        sub, E.QName(A, "NameID"), Format="urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified"
    ).text = subject
    conf = E.SubElement(sub, E.QName(A, "SubjectConfirmation"), Method="urn:oasis:names:tc:SAML:2.0:cm:bearer")
    E.SubElement(
        conf, E.QName(A, "SubjectConfirmationData"), InResponseTo=request_id, Recipient=acs, NotOnOrAfter=stamp(until)
    )
    conditions = E.SubElement(
        assertion, E.QName(A, "Conditions"), NotBefore=stamp(now - timedelta(minutes=1)), NotOnOrAfter=stamp(until)
    )
    restriction = E.SubElement(conditions, E.QName(A, "AudienceRestriction"))
    E.SubElement(restriction, E.QName(A, "Audience")).text = audience
    statement = E.SubElement(
        assertion, E.QName(A, "AuthnStatement"), AuthnInstant=stamp(now), SessionIndex="_" + uuid.uuid4().hex
    )
    context = E.SubElement(statement, E.QName(A, "AuthnContext"))
    E.SubElement(
        context, E.QName(A, "AuthnContextClassRef")
    ).text = "urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport"
    ast = E.SubElement(assertion, E.QName(A, "AttributeStatement"))
    for name, values in attrs.items():
        attr = E.SubElement(ast, E.QName(A, "Attribute"), Name=name)
        for value in values if isinstance(values, list) else [values]:
            E.SubElement(attr, E.QName(A, "AttributeValue")).text = value
    if variant == "missing_correlation":
        response.attrib.pop("InResponseTo")
        conf[0].attrib.pop("InResponseTo")
    if signature in ("assertion", "both", "legacy"):
        signed = OneLogin_Saml2_Utils.add_sign(
            E.tostring(assertion),
            key,
            cert,
            **(
                {
                    "sign_algorithm": "http://www.w3.org/2000/09/xmldsig#rsa-sha1",
                    "digest_algorithm": "http://www.w3.org/2000/09/xmldsig#sha1",
                }
                if signature == "legacy"
                else {}
            ),
        )
        response.replace(assertion, E.fromstring(signed))
    if signature in ("response", "both"):
        raw = OneLogin_Saml2_Utils.add_sign(E.tostring(response), key, cert)
    else:
        raw = E.tostring(response)
    if variant == "wrapping":
        from copy import deepcopy

        wrapped = E.fromstring(raw)
        wrapped.append(deepcopy(wrapped.find(E.QName(A, "Assertion"))))
        raw = E.tostring(wrapped)
    if variant == "entity":
        raw = b'<!DOCTYPE samlp:Response [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>' + raw
    if variant == "tampered":
        raw = raw.replace(b"Sandbox Person", b"Forged Person")
    return base64.b64encode(raw).decode()


def encrypt_assertion(encoded, sp_cert):
    import xmlsec

    root = E.fromstring(base64.b64decode(encoded))
    assertion = root.find(E.QName(A, "Assertion"))
    manager = xmlsec.KeysManager()
    manager.add_key(xmlsec.Key.from_memory(sp_cert, xmlsec.KeyFormat.CERT_PEM, None))
    encrypted = xmlsec.template.encrypted_data_create(
        root, xmlsec.Transform.AES256_GCM, type=xmlsec.EncryptionType.ELEMENT, ns="xenc"
    )
    xmlsec.template.encrypted_data_ensure_cipher_value(encrypted)
    info = xmlsec.template.encrypted_data_ensure_key_info(encrypted, ns="ds")
    key = xmlsec.template.add_encrypted_key(info, xmlsec.Transform.RSA_OAEP)
    xmlsec.template.encrypted_data_ensure_cipher_value(key)
    ctx = xmlsec.EncryptionContext(manager)
    ctx.key = xmlsec.Key.generate(xmlsec.KeyData.AES, 256, xmlsec.KeyDataType.SESSION)
    encrypted = ctx.encrypt_xml(encrypted, assertion)
    wrapper = E.Element(E.QName(A, "EncryptedAssertion"), nsmap={"saml": A})
    root.replace(encrypted, wrapper)
    wrapper.append(encrypted)
    return base64.b64encode(E.tostring(root)).decode()
