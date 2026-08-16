from execution.secret_redaction import REDACTED, mask_account_number, redact_text, redact_value


class TestMaskAccountNumber:
    def test_shows_only_last_4_digits(self):
        assert mask_account_number("12345678") == "****5678"

    def test_none_passes_through(self):
        assert mask_account_number(None) is None

    def test_empty_string_passes_through(self):
        assert mask_account_number("") == ""

    def test_short_value_fully_masked(self):
        assert mask_account_number("123") == "***"

    def test_non_string_input_coerced(self):
        assert mask_account_number(12345678) == "****5678"


class TestRedactValue:
    def test_redacts_sensitive_keys_case_insensitive(self):
        payload = {"AppKey": "secret1", "AppSecret": "secret2", "other": "keep-me"}
        redacted = redact_value(payload)
        assert redacted["AppKey"] == REDACTED
        assert redacted["AppSecret"] == REDACTED
        assert redacted["other"] == "keep-me"

    def test_redacts_nested_dicts(self):
        payload = {"headers": {"Authorization": "Bearer xyz"}, "body": {"cano": "12345678"}}
        redacted = redact_value(payload)
        assert redacted["headers"]["Authorization"] == REDACTED
        assert redacted["body"]["cano"] == REDACTED

    def test_redacts_within_lists(self):
        payload = {"items": [{"access_token": "abc"}, {"symbol": "AAPL"}]}
        redacted = redact_value(payload)
        assert redacted["items"][0]["access_token"] == REDACTED
        assert redacted["items"][1]["symbol"] == "AAPL"

    def test_all_named_sensitive_keys_covered(self):
        payload = {
            "appkey": "1", "appsecret": "2", "authorization": "3",
            "access_token": "4", "account_number": "5", "cano": "6", "token": "7",
        }
        redacted = redact_value(payload)
        assert all(v == REDACTED for v in redacted.values())

    def test_non_dict_non_list_passthrough(self):
        assert redact_value("plain string") == "plain string"
        assert redact_value(42) == 42


class TestRedactText:
    def test_redacts_key_equals_value_fragment(self):
        text = "KIS token issuance failed: appkey=ABCD1234XYZ appsecret=SECRETVALUE"
        redacted = redact_text(text)
        assert "ABCD1234XYZ" not in redacted
        assert "SECRETVALUE" not in redacted
        assert REDACTED in redacted

    def test_redacts_json_style_fragment(self):
        text = '{"CANO": "12345678", "ACNT_PRDT_CD": "01"}'
        redacted = redact_text(text)
        assert "12345678" not in redacted

    def test_non_string_passthrough(self):
        assert redact_text(None) is None
        assert redact_text(42) == 42

    def test_text_without_sensitive_fragments_unchanged(self):
        text = "insufficient KIS orderable cash: need $100.00, have $50.00"
        assert redact_text(text) == text
