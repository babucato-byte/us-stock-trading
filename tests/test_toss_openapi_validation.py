import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from validation.toss_openapi.client import (
    BLOCKED_MUTATING_REQUEST,
    MissingCredentialError,
    TossReadOnlyClient,
    ValidationError,
    mask_identifier,
    safe_error_message,
)


class TossValidationSafetyTests(unittest.TestCase):
    def test_credentials_are_never_logged(self):
        client_id = "client-id-secret"
        client_secret = "client-secret-secret"
        client = TossReadOnlyClient(client_id, client_secret)
        output = io.StringIO()
        class FakeClient:
            def authenticate(self):
                raise ValidationError("credentials %s %s" % (client_id, client_secret))
        with patch("validation.toss_openapi.validate.TossReadOnlyClient", return_value=FakeClient()):
            from validation.toss_openapi.validate import main
            with redirect_stdout(output):
                main()
        self.assertNotIn(client.client_id, output.getvalue())
        self.assertNotIn(client.client_secret, output.getvalue())

    def test_token_is_never_logged(self):
        token = "very-sensitive-access-token"
        output = io.StringIO()
        class FakeClient:
            access_token = token
            def authenticate(self):
                return SimpleNamespace(status=200, latency_ms=1)
            def accounts(self):
                raise ValidationError("token %s" % token)
        with patch("validation.toss_openapi.validate.TossReadOnlyClient", return_value=FakeClient()):
            from validation.toss_openapi.validate import main
            with redirect_stdout(output):
                main()
        self.assertNotIn(token, output.getvalue())

    def test_account_number_is_masked(self):
        masked = mask_identifier("123456789012")
        self.assertEqual(masked, "********9012")
        self.assertNotIn("123456789012", masked)

    def test_mutating_endpoints_are_blocked(self):
        client = TossReadOnlyClient("id", "secret")
        for method, path in (("POST", "/api/v1/orders"), ("DELETE", "/api/v1/conditional-orders/1"), ("GET", "/api/v1/orders")):
            with self.assertRaisesRegex(ValidationError, BLOCKED_MUTATING_REQUEST):
                client._request(method, path)

    def test_missing_credential_fails_safely(self):
        with patch.dict("os.environ", {"TOSS_CLIENT_ID": "", "TOSS_CLIENT_SECRET": ""}, clear=False):
            with self.assertRaisesRegex(MissingCredentialError, "MISSING_TOSS_CREDENTIALS"):
                TossReadOnlyClient().authenticate()

    def test_http_error_does_not_leak_authorization_header(self):
        client = TossReadOnlyClient("id", "secret")
        client.access_token = "token-that-must-not-leak"
        error = HTTPError("https://example.invalid", 401, "Unauthorized", {"Authorization": "Bearer leaked"}, None)
        with patch("validation.toss_openapi.client.urlopen", side_effect=error):
            with self.assertRaises(ValidationError) as raised:
                client.accounts()
        message = str(raised.exception)
        self.assertEqual(message, "HTTP_ERROR status=401")
        self.assertNotIn("Bearer", message)
        self.assertNotIn(client.access_token, message)


if __name__ == "__main__":
    unittest.main()
