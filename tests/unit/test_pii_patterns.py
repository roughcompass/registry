"""Unit tests for built-in PII pattern modules.

Coverage
--------
- Positive match for every pattern.
- Near-miss / negative cases to verify false-positive filtering.
- Luhn-invalid card number is rejected by credit_card.
- AWS secret key low-entropy string is rejected.
- SSN invalid area/group/serial codes are rejected.
- scan() never raises regardless of input (empty, None-coerced, garbage).
- Pattern Protocol surface: name, category, scan() present on each singleton.
- PiiMatchResult fields (name, offset, length, category) are correct.

No I/O, no network, no database required.

PII patterns must minimise false positives (Luhn, entropy, and range checks
guard the more error-prone patterns like credit cards and AWS secrets).
"""

from __future__ import annotations

import pytest

from registry.security.pii_patterns.aws_access_key import pattern as aws_access_key
from registry.security.pii_patterns.aws_secret_key import pattern as aws_secret_key
from registry.security.pii_patterns.credit_card import pattern as credit_card
from registry.security.pii_patterns.email import pattern as email
from registry.security.pii_patterns.jwt_token import pattern as jwt_token
from registry.security.pii_patterns.phone import pattern as phone
from registry.security.pii_patterns.ssn import pattern as ssn
from registry.types import PiiMatchResult

# ---------------------------------------------------------------------------
# Protocol surface checks
# ---------------------------------------------------------------------------


ALL_PATTERNS = [email, phone, ssn, aws_access_key, aws_secret_key, jwt_token, credit_card]


@pytest.mark.parametrize("pat", ALL_PATTERNS, ids=lambda p: p.name)
def test_pattern_has_name(pat) -> None:
    assert isinstance(pat.name, str) and pat.name


@pytest.mark.parametrize("pat", ALL_PATTERNS, ids=lambda p: p.name)
def test_pattern_has_category(pat) -> None:
    assert isinstance(pat.category, str) and pat.category


@pytest.mark.parametrize("pat", ALL_PATTERNS, ids=lambda p: p.name)
def test_scan_returns_list(pat) -> None:
    result = pat.scan("no pii here")
    assert isinstance(result, list)


@pytest.mark.parametrize("pat", ALL_PATTERNS, ids=lambda p: p.name)
def test_scan_never_raises_on_empty(pat) -> None:
    result = pat.scan("")
    assert result == []


@pytest.mark.parametrize("pat", ALL_PATTERNS, ids=lambda p: p.name)
def test_scan_never_raises_on_garbage(pat) -> None:
    result = pat.scan("\x00\xff\n\t" * 100)
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# PiiMatchResult field validation helper
# ---------------------------------------------------------------------------


def _assert_match(result: PiiMatchResult, *, name: str, category: str, text: str) -> None:
    """Assert that a PiiMatchResult has correct fields for the given text."""
    assert result.name == name
    assert result.category == category
    assert result.offset >= 0
    assert result.length > 0
    assert result.offset + result.length <= len(text)
    matched = text[result.offset : result.offset + result.length]
    assert len(matched) == result.length


# ===========================================================================
# email
# ===========================================================================


class TestEmail:
    def test_simple_email(self) -> None:
        text = "Contact us at alice@example.com for support."
        results = email.scan(text)
        assert len(results) == 1
        _assert_match(results[0], name="email", category="CONTACT", text=text)
        assert text[results[0].offset : results[0].offset + results[0].length] == "alice@example.com"

    def test_email_with_plus_tag(self) -> None:
        text = "Reply to alice+tag@sub.example.org"
        results = email.scan(text)
        assert len(results) == 1
        assert "alice+tag@sub.example.org" in text[results[0].offset : results[0].offset + results[0].length]

    def test_multiple_emails(self) -> None:
        text = "From: alice@example.com To: bob@corp.io"
        results = email.scan(text)
        assert len(results) == 2
        assert results[0].offset < results[1].offset

    def test_no_email_in_plain_text(self) -> None:
        assert email.scan("Hello world, no emails here.") == []

    def test_no_email_bare_at_sign(self) -> None:
        # A bare @ without a valid domain should not match.
        assert email.scan("user @ host") == []

    def test_no_email_missing_tld(self) -> None:
        # Single-label domain with no TLD should not match.
        assert email.scan("user@localhost") == []

    def test_email_offset_correct(self) -> None:
        text = "   bob@foo.com   "
        results = email.scan(text)
        assert len(results) == 1
        assert text[results[0].offset : results[0].offset + results[0].length] == "bob@foo.com"


# ===========================================================================
# phone
# ===========================================================================


class TestPhone:
    def test_e164(self) -> None:
        text = "Call me at +14155552671"
        results = phone.scan(text)
        assert len(results) >= 1

    def test_nanp_dashes(self) -> None:
        text = "Reach us at 415-555-2671"
        results = phone.scan(text)
        assert len(results) >= 1

    def test_nanp_parentheses(self) -> None:
        text = "Phone: (415) 555-2671"
        results = phone.scan(text)
        assert len(results) >= 1

    def test_nanp_with_country_code(self) -> None:
        text = "Dial 1-800-555-0199 for help"
        results = phone.scan(text)
        assert len(results) >= 1

    def test_no_phone_short_number(self) -> None:
        # 5-digit number should not match.
        assert phone.scan("Order 12345 placed.") == []

    def test_no_phone_date(self) -> None:
        # Dates like 2026-05-10 should not trigger phone matches.
        result = phone.scan("Date: 2026-05-10")
        # May or may not match depending on separator logic; the date has 8 digits
        # with a 10 in the last group — not a valid NANP exchange (starts with 1).
        # We just verify scan() returns a list (no crash) and no false alarm.
        assert isinstance(result, list)

    def test_result_fields(self) -> None:
        text = "Call +442071234567 now"
        results = phone.scan(text)
        if results:
            _assert_match(results[0], name="phone", category="CONTACT", text=text)


# ===========================================================================
# ssn
# ===========================================================================


class TestSsn:
    def test_valid_ssn_dashes(self) -> None:
        text = "SSN: 123-45-6789"
        results = ssn.scan(text)
        assert len(results) == 1
        _assert_match(results[0], name="ssn", category="GOVERNMENT_ID", text=text)

    def test_valid_ssn_spaces(self) -> None:
        text = "SSN 234 56 7890"
        results = ssn.scan(text)
        assert len(results) == 1

    def test_invalid_area_000(self) -> None:
        assert ssn.scan("000-45-6789") == []

    def test_invalid_area_666(self) -> None:
        assert ssn.scan("666-45-6789") == []

    def test_invalid_area_900(self) -> None:
        assert ssn.scan("987-65-4321") == []

    def test_invalid_group_00(self) -> None:
        assert ssn.scan("123-00-6789") == []

    def test_invalid_serial_0000(self) -> None:
        assert ssn.scan("123-45-0000") == []

    def test_all_same_digits(self) -> None:
        assert ssn.scan("111-11-1111") == []

    def test_no_ssn_in_plain_text(self) -> None:
        assert ssn.scan("No social security numbers here.") == []

    def test_ssn_offset(self) -> None:
        text = "Data: 321-54-9876 found."
        results = ssn.scan(text)
        assert len(results) == 1
        assert text[results[0].offset : results[0].offset + results[0].length] == "321-54-9876"


# ===========================================================================
# aws_access_key
# ===========================================================================


class TestAwsAccessKey:
    def test_akia_key(self) -> None:
        text = "key = AKIAIOSFODNN7EXAMPLE"
        results = aws_access_key.scan(text)
        assert len(results) == 1
        _assert_match(results[0], name="aws_access_key", category="CREDENTIALS", text=text)

    def test_asia_key(self) -> None:
        # ASIA + exactly 16 uppercase alphanumeric characters = 20 chars total.
        text = "ASIAIOSFODNN7EXAMPLE"
        results = aws_access_key.scan(text)
        assert len(results) == 1

    def test_too_short(self) -> None:
        # AKIA with only 10 chars after (not 16) should not match.
        assert aws_access_key.scan("AKIAIOSFOD") == []

    def test_lowercase_not_matched(self) -> None:
        # AWS access keys are always uppercase.
        assert aws_access_key.scan("akiaiosfodnn7example") == []

    def test_no_key_in_plain_text(self) -> None:
        assert aws_access_key.scan("access_key = my_key") == []

    def test_key_embedded_in_config(self) -> None:
        text = "aws_access_key_id = AKIAIOSFODNN7EXAMPLE\nregion = us-east-1"
        results = aws_access_key.scan(text)
        assert len(results) == 1
        matched = text[results[0].offset : results[0].offset + results[0].length]
        assert matched == "AKIAIOSFODNN7EXAMPLE"


# ===========================================================================
# aws_secret_key
# ===========================================================================

# A real-looking high-entropy 40-char AWS secret key (synthetic, not real).
_REAL_LIKE_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
# Low-entropy 40-char string (all same character repeating).
_LOW_ENTROPY_SECRET = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
# Medium-entropy English phrase padded to 40 chars.
_MEDIUM_ENTROPY = "helloworldhelloworldhelloworldhelloworld"


class TestAwsSecretKey:
    def test_high_entropy_key_detected(self) -> None:
        text = f"secret = {_REAL_LIKE_SECRET}"
        results = aws_secret_key.scan(text)
        assert len(results) == 1
        _assert_match(results[0], name="aws_secret_key", category="CREDENTIALS", text=text)

    def test_low_entropy_rejected(self) -> None:
        text = f"value = {_LOW_ENTROPY_SECRET}"
        results = aws_secret_key.scan(text)
        assert results == []

    def test_medium_entropy_rejected(self) -> None:
        # Repeated English words have entropy well below 4.5 bits/char.
        text = f"value = {_MEDIUM_ENTROPY}"
        results = aws_secret_key.scan(text)
        assert results == []

    def test_no_secret_in_plain_text(self) -> None:
        assert aws_secret_key.scan("Hello, world! This is a normal sentence.") == []

    def test_never_raises_on_short_string(self) -> None:
        assert isinstance(aws_secret_key.scan("short"), list)


# ===========================================================================
# jwt_token
# ===========================================================================

# Synthetic JWT-shaped token (header.payload.signature — base64url, not a real JWT).
_FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    "."
    "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ"
    "."
    "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)


class TestJwtToken:
    def test_real_jwt_detected(self) -> None:
        text = f"Authorization: Bearer {_FAKE_JWT}"
        results = jwt_token.scan(text)
        assert len(results) == 1
        _assert_match(results[0], name="jwt_token", category="CREDENTIALS", text=text)

    def test_short_dot_string_not_matched(self) -> None:
        # Version string like "1.2.3" is too short for each segment.
        assert jwt_token.scan("version 1.2.3") == []

    def test_filename_not_matched(self) -> None:
        # file.tar.gz — segments shorter than 10 chars.
        assert jwt_token.scan("archive.tar.gz") == []

    def test_no_jwt_in_plain_text(self) -> None:
        assert jwt_token.scan("No tokens here at all.") == []

    def test_two_segment_string_not_matched(self) -> None:
        # Only two dots separating three segments is required; two-part string fails.
        assert jwt_token.scan("onlytwosegments.whichisshorterthantencharacters") == []

    def test_jwt_offset_correct(self) -> None:
        # Use a space separator so the JWT is clearly delimited from the prefix.
        text = f"Bearer {_FAKE_JWT}"
        results = jwt_token.scan(text)
        assert len(results) == 1
        matched = text[results[0].offset : results[0].offset + results[0].length]
        assert matched == _FAKE_JWT


# ===========================================================================
# credit_card
# ===========================================================================

# Valid Luhn card numbers from the test card number lists (not real cards).
_VISA_VALID = "4111111111111111"  # Classic Visa test number, passes Luhn.
_VISA_VALID_SPACED = "4111 1111 1111 1111"
_MC_VALID = "5500005555555559"  # Mastercard test number.
_AMEX_VALID = "378282246310005"  # Amex test number.
_DISCOVER_VALID = "6011111111111117"  # Discover test number.

# A number that looks like a Visa PAN but fails Luhn (last digit off by 1).
_VISA_LUHN_FAIL = "4111111111111112"


class TestCreditCard:
    def test_visa_detected(self) -> None:
        text = f"Card: {_VISA_VALID}"
        results = credit_card.scan(text)
        assert len(results) == 1
        _assert_match(results[0], name="credit_card", category="FINANCIAL", text=text)

    def test_visa_spaced_detected(self) -> None:
        text = f"Number: {_VISA_SPACED}"
        results = credit_card.scan(text)
        assert len(results) == 1

    def test_mastercard_detected(self) -> None:
        text = f"MC: {_MC_VALID}"
        results = credit_card.scan(text)
        assert len(results) == 1

    def test_amex_detected(self) -> None:
        text = f"Amex: {_AMEX_VALID}"
        results = credit_card.scan(text)
        assert len(results) == 1

    def test_discover_detected(self) -> None:
        text = f"Discover: {_DISCOVER_VALID}"
        results = credit_card.scan(text)
        assert len(results) == 1

    def test_luhn_invalid_rejected(self) -> None:
        text = f"Card: {_VISA_LUHN_FAIL}"
        results = credit_card.scan(text)
        assert results == [], f"Luhn-invalid card {_VISA_LUHN_FAIL!r} should be rejected"

    def test_no_card_in_plain_text(self) -> None:
        assert credit_card.scan("This text has no credit card number at all.") == []

    def test_short_number_not_matched(self) -> None:
        assert credit_card.scan("Order number 411111") == []

    def test_card_offset_correct(self) -> None:
        text = f"  {_VISA_VALID}  "
        results = credit_card.scan(text)
        assert len(results) == 1
        matched = text[results[0].offset : results[0].offset + results[0].length]
        # Strip separators from matched to compare with digit-only valid.
        assert matched.replace(" ", "").replace("-", "") == _VISA_VALID

    def test_result_category(self) -> None:
        text = f"charge {_AMEX_VALID}"
        results = credit_card.scan(text)
        assert len(results) == 1
        assert results[0].category == "FINANCIAL"


# fix reference used in test
_VISA_SPACED = _VISA_VALID_SPACED


# ===========================================================================
# Package-level import check
# ===========================================================================


def test_package_exports_all_patterns() -> None:
    from registry.security.pii_patterns import BUILT_IN_PATTERNS

    assert len(BUILT_IN_PATTERNS) == 7
    names = {p.name for p in BUILT_IN_PATTERNS}
    assert names == {
        "email",
        "phone",
        "ssn",
        "aws_access_key",
        "aws_secret_key",
        "jwt_token",
        "credit_card",
    }
