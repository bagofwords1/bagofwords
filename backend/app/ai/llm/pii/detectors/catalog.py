"""The entity types a policy can turn on for a detector.

Three kinds:

* ``ner`` — names, places, organizations: what only a language model finds.
* ``pattern`` — identifiers Presidio recognizes with a pattern **and a
  checksum** (Luhn for cards, mod-97 for IBANs, the national check digits for
  ID numbers). A match that fails the checksum is dropped and one that passes
  scores 1.0, so these stay precise at the default threshold. That is the
  difference from the built-in regex rules, which match any number of the
  right shape: Presidio's ``CREDIT_CARD`` matches about one 13–16 digit number
  in ten (the ones that pass Luhn), the built-in ``credit_card`` rule every one.

* ``context`` — Presidio's recognizers without a checksum (US passport,
  driver's license, bank account…). They score 0.01–0.5 and gain 0.35 when a
  context word ("passport", "account"…) is near the value, so they carry their
  own lower default sensitivity (``min_score``, 0.4): weak patterns then need
  the context word, stronger ones do not.

Left out: India GSTIN (Presidio's recognizer accepts a wrong check character
at full confidence) and the German passport / ID card recognizers (one check
digit: about 5% of letter-plus-8-digit product codes score 1.0, so no
threshold separates them); types the
built-in rules already cover (email, IP, MAC) and ones
that would rewrite ordinary analytics text (dates, URLs, UUIDs).

``recognizer`` is the class in ``presidio_analyzer.predefined_recognizers``
the in-process detector loads for the entity.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

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
    # Tax / VAT numbers with a check digit
    _pattern("IT_VAT_CODE", "Italy VAT number (partita IVA)", "[REDACTED_TAX_ID]", "ItVatCodeRecognizer"),
    _pattern("DE_VAT_ID", "Germany VAT ID (USt-IdNr.)", "[REDACTED_TAX_ID]", "DeVatIdRecognizer"),
]

# Default sensitivity for ``context`` entities: a weak pattern (score
# 0.01–0.3) reaches it only with a context word nearby (+0.35, e.g.
# "passport" next to the number); a stronger one (0.5) without.
CONTEXT_MIN_SCORE = 0.4


def _context(entity_id: str, name: str, replacement: str, recognizer: str, min_score: float = CONTEXT_MIN_SCORE) -> Dict[str, Any]:
    return {"id": entity_id, "name": name, "replacement": replacement, "kind": "context",
            "recognizer": recognizer, "min_score": min_score}


# Presidio's recognizers without a checksum. Most match only when a context
# word is near the value, which holds in prose and often not in table rows
# (a column header is rarely next to the cell).
CONTEXT_ENTITIES: List[Dict[str, Any]] = [
    # libphonenumber scores any plausible number 0.4 (prices included): only
    # with a word like "phone" or "tel" nearby (0.75).
    _context("PHONE_NUMBER", "Phone number (international, libphonenumber)", "[REDACTED_PHONE]", "PhoneRecognizer", min_score=0.7),
    _context("US_SSN", "US Social Security number", "[REDACTED_SSN]", "UsSsnRecognizer"),
    _context("US_ITIN", "US Individual Taxpayer ID (ITIN)", "[REDACTED_TAX_ID]", "UsItinRecognizer"),
    _context("US_PASSPORT", "US passport number", "[REDACTED_PASSPORT]", "UsPassportRecognizer"),
    _context("US_DRIVER_LICENSE", "US driver's license number", "[REDACTED_LICENSE]", "UsLicenseRecognizer"),
    _context("US_BANK_NUMBER", "US bank account number", "[REDACTED_BANK_ACCOUNT]", "UsBankRecognizer"),
    _context("US_MBI", "US Medicare Beneficiary ID (MBI)", "[REDACTED_MBI]", "UsMbiRecognizer"),
    _context("UK_NINO", "UK National Insurance number", "[REDACTED_NINO]", "UkNinoRecognizer"),
    _context("UK_PASSPORT", "UK passport number", "[REDACTED_PASSPORT]", "UkPassportRecognizer"),
    _context("UK_DRIVING_LICENCE", "UK driving licence number", "[REDACTED_LICENSE]", "UkDrivingLicenceRecognizer"),
    _context("UK_POSTCODE", "UK postcode", "[REDACTED_POSTCODE]", "UkPostcodeRecognizer"),
    _context("CA_SIN", "Canada Social Insurance Number", "[REDACTED_SIN]", "CaSinRecognizer"),
    _context("SG_NRIC_FIN", "Singapore NRIC / FIN", "[REDACTED_ID]", "SgFinRecognizer"),
    _context("IN_PAN", "India PAN", "[REDACTED_TAX_ID]", "InPanRecognizer"),
    _context("IN_PASSPORT", "India passport number", "[REDACTED_PASSPORT]", "InPassportRecognizer"),
    # Three letters + seven digits looks like many product codes: context only.
    _context("IN_VOTER", "India voter ID (EPIC)", "[REDACTED_ID]", "InVoterRecognizer", min_score=0.7),
    _context("ES_PASSPORT", "Spain passport number", "[REDACTED_PASSPORT]", "EsPassportRecognizer"),
    _context("IT_DRIVER_LICENSE", "Italy driver's license number", "[REDACTED_LICENSE]", "ItDriverLicenseRecognizer"),
    _context("IT_PASSPORT", "Italy passport number", "[REDACTED_PASSPORT]", "ItPassportRecognizer"),
    _context("IT_IDENTITY_CARD", "Italy identity card number", "[REDACTED_ID]", "ItIdentityCardRecognizer"),
    _context("KR_PASSPORT", "South Korea passport number", "[REDACTED_PASSPORT]", "KrPassportRecognizer"),
    _context("PH_PASSPORT", "Philippines passport number", "[REDACTED_PASSPORT]", "PhPassportRecognizer"),
]

DETECTOR_ENTITIES: List[Dict[str, Any]] = NER_ENTITIES + PATTERN_ENTITIES + CONTEXT_ENTITIES
# Every entity the in-process detector loads a recognizer class for.
RECOGNIZER_ENTITIES: List[Dict[str, Any]] = PATTERN_ENTITIES + CONTEXT_ENTITIES

NER_ENTITY_IDS = [e["id"] for e in NER_ENTITIES]

DEFAULT_MIN_SCORE = 0.7


def default_min_score(entity_id: str) -> float:
    """The sensitivity an entity uses until a policy sets its own."""
    spec = detector_entity(entity_id)
    return float(spec.get("min_score", DEFAULT_MIN_SCORE)) if spec else DEFAULT_MIN_SCORE


def detector_entity(entity_id: str) -> Optional[Dict[str, Any]]:
    for e in DETECTOR_ENTITIES:
        if e["id"] == entity_id:
            return e
    return None
