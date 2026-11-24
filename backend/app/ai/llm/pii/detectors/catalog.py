"""The entity types a policy can turn on for a detector.

Two kinds:

* ``ner`` — names, places, organizations: what only a language model finds.
* ``pattern`` — identifiers Presidio recognizes with a pattern **and a
  checksum** (Luhn for cards, mod-97 for IBANs, the national check digits for
  ID numbers). A match that fails the checksum is dropped and one that passes
  scores 1.0, so these stay precise at the default threshold. That is the
  difference from the built-in regex rules, which match any number of the
  right shape: Presidio's ``CREDIT_CARD`` matches about one 13–16 digit number
  in ten (the ones that pass Luhn), the built-in ``credit_card`` rule every one.

Presidio's pattern recognizers without a checksum (US passport, driver's
license, bank account…) are left out: they score 0.05–0.5 and only pass the
threshold with context words, so they would read as on while finding nothing.

``recognizer`` is the class in ``presidio_analyzer.predefined_recognizers``
the in-process detector loads for the entity.
"""

from __future__ import annotations

from typing import Dict, List, Optional

DETECTOR_PREFIX = "presidio."

NER_ENTITIES: List[Dict[str, str]] = [
    {"id": "PERSON", "name": "Person name", "replacement": "[REDACTED_PERSON]", "kind": "ner"},
    {"id": "LOCATION", "name": "Location", "replacement": "[REDACTED_LOCATION]", "kind": "ner"},
    {"id": "ORGANIZATION", "name": "Organization", "replacement": "[REDACTED_ORG]", "kind": "ner"},
    {"id": "NRP", "name": "Nationality, religious or political group", "replacement": "[REDACTED_GROUP]", "kind": "ner"},
]


def _pattern(entity_id: str, name: str, replacement: str, recognizer: str) -> Dict[str, str]:
    return {"id": entity_id, "name": name, "replacement": replacement, "kind": "pattern", "recognizer": recognizer}


PATTERN_ENTITIES: List[Dict[str, str]] = [
    # Financial
    _pattern("CREDIT_CARD", "Credit card number (Luhn-checked)", "[REDACTED_CC]", "CreditCardRecognizer"),
    _pattern("IBAN_CODE", "IBAN (checksum-validated)", "[REDACTED_IBAN]", "IbanRecognizer"),
    _pattern("ABA_ROUTING_NUMBER", "US bank routing number (ABA)", "[REDACTED_ROUTING]", "AbaRoutingRecognizer"),
    _pattern("CRYPTO", "Crypto wallet address (Bitcoin)", "[REDACTED_CRYPTO]", "CryptoRecognizer"),
    # Healthcare (US / UK)
    _pattern("US_NPI", "US National Provider Identifier (NPI)", "[REDACTED_NPI]", "UsNpiRecognizer"),
    _pattern("MEDICAL_LICENSE", "US DEA registration number", "[REDACTED_MED_LICENSE]", "MedicalLicenseRecognizer"),
    _pattern("UK_NHS", "UK NHS number", "[REDACTED_NHS]", "NhsRecognizer"),
    # National identifiers
    _pattern("ES_NIF", "Spain NIF / DNI", "[REDACTED_ID]", "EsNifRecognizer"),
    _pattern("ES_NIE", "Spain NIE (foreigner ID)", "[REDACTED_ID]", "EsNieRecognizer"),
    _pattern("IT_FISCAL_CODE", "Italy fiscal code (codice fiscale)", "[REDACTED_ID]", "ItFiscalCodeRecognizer"),
    _pattern("DE_TAX_ID", "Germany tax ID (Steuer-ID)", "[REDACTED_TAX_ID]", "DeTaxIdRecognizer"),
    _pattern("PL_PESEL", "Poland PESEL", "[REDACTED_ID]", "PlPeselRecognizer"),
    _pattern("FI_PERSONAL_IDENTITY_CODE", "Finland personal identity code", "[REDACTED_ID]", "FiPersonalIdentityCodeRecognizer"),
    _pattern("SE_PERSONNUMMER", "Sweden personnummer", "[REDACTED_ID]", "SePersonnummerRecognizer"),
    _pattern("TR_NATIONAL_ID", "Turkey national ID (TC Kimlik)", "[REDACTED_ID]", "TrNationalIdRecognizer"),
    _pattern("AU_TFN", "Australia tax file number (TFN)", "[REDACTED_TAX_ID]", "AuTfnRecognizer"),
    _pattern("AU_ABN", "Australia business number (ABN)", "[REDACTED_ABN]", "AuAbnRecognizer"),
    _pattern("AU_MEDICARE", "Australia Medicare number", "[REDACTED_MEDICARE]", "AuMedicareRecognizer"),
    _pattern("IN_AADHAAR", "India Aadhaar number", "[REDACTED_ID]", "InAadhaarRecognizer"),
    _pattern("KR_RRN", "South Korea resident registration number", "[REDACTED_ID]", "KrRrnRecognizer"),
    _pattern("ZA_ID_NUMBER", "South Africa ID number", "[REDACTED_ID]", "ZaIdNumberRecognizer"),
]

DETECTOR_ENTITIES: List[Dict[str, str]] = NER_ENTITIES + PATTERN_ENTITIES

NER_ENTITY_IDS = [e["id"] for e in NER_ENTITIES]

DEFAULT_MIN_SCORE = 0.7


def detector_entity(entity_id: str) -> Optional[Dict[str, str]]:
    for e in DETECTOR_ENTITIES:
        if e["id"] == entity_id:
            return e
    return None
