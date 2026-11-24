import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import bridge  # noqa: E402

try:
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError:  # the OIDC tests need the packages of requirements.txt
    jwt = None

AUDIENCE = "agent-a-client-id"


def new_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@unittest.skipUnless(jwt, "PyJWT[crypto] is not installed")
class OidcTest(unittest.TestCase):
    """The bridge against a fake identity provider shaped like Authentik's."""

    @classmethod
    def setUpClass(cls):
        cls.key = new_key()
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key()))
        cls.jwk.update(kid="k1", use="sig", alg="RS256")
        test = cls

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                base = f"http://127.0.0.1:{self.server.server_port}/application/o/a2a/"
                if self.path == "/application/o/a2a/.well-known/openid-configuration":
                    body = {"issuer": base, "jwks_uri": base + "jwks/"}
                elif self.path == "/application/o/a2a/jwks/":
                    body = {"keys": [test.jwk]}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        cls.idp = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        threading.Thread(target=cls.idp.serve_forever, daemon=True).start()
        cls.issuer = f"http://127.0.0.1:{cls.idp.server_port}/application/o/a2a/"

    @classmethod
    def tearDownClass(cls):
        cls.idp.shutdown()
        cls.idp.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def bridge(self, **env):
        values = {"A2A_DATA_DIR": self.tmp, "A2A_OIDC_ISSUER": self.issuer,
                  "A2A_OIDC_AUDIENCE": AUDIENCE, **env}
        return bridge.Bridge(bridge.Config(values))

    def token(self, key=None, headers=None, **claims):
        payload = {"iss": self.issuer, "sub": "svc-agent-a", "aud": AUDIENCE, "azp": AUDIENCE,
                   "exp": int(time.time()) + 300, "scope": "openid a2a", **claims}
        payload = {k: v for k, v in payload.items() if v is not None}
        return jwt.encode(payload, key or self.key, algorithm="RS256", headers={"kid": "k1", **(headers or {})})

    def auth(self, b, token):
        return b.authenticate(f"Bearer {token}")

    def refused(self, b, token, status):
        with self.assertRaises(bridge.AuthError) as ctx:
            self.auth(b, token)
        self.assertEqual(ctx.exception.status, status)
        return ctx.exception

    def test_valid_token(self):
        self.assertEqual(self.auth(self.bridge(), self.token()), AUDIENCE)

    def test_caller_is_written_in_the_inbox_file(self):
        b = self.bridge()
        caller = self.auth(b, self.token())
        request = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
            "message": {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "hi"}]},
            "configuration": {"returnImmediately": True}}}
        task = b.handle(request, None, caller)["result"]["task"]
        content = (Path(self.tmp) / "inbox" / f"a2a-{task['id']}.md").read_text(encoding="utf-8")
        self.assertIn(f"Authenticated caller: {AUDIENCE}.", content)

    def test_rejected_tokens(self):
        b = self.bridge()
        self.refused(b, self.token(iss="https://evil.example/"), 401)
        self.refused(b, self.token(aud="someone-else"), 401)
        self.refused(b, self.token(aud=None), 401)
        self.refused(b, self.token(exp=int(time.time()) - 3600), 401)
        self.refused(b, self.token(exp=None), 401)
        self.refused(b, self.token(key=new_key()), 401)  # right kid, wrong key
        self.refused(b, self.token(headers={"kid": "unknown"}), 401)
        self.refused(b, "not.a.jwt", 401)

    def test_alg_none_and_hmac_confusion_are_rejected(self):
        b = self.bridge()
        forged = jwt.encode({"iss": self.issuer, "sub": "x", "aud": AUDIENCE, "exp": int(time.time()) + 300},
                            None, algorithm="none", headers={"kid": "k1"})
        self.refused(b, forged, 401)
        public_pem = self.key.public_key().public_bytes(
            __import__("cryptography").hazmat.primitives.serialization.Encoding.PEM,
            __import__("cryptography").hazmat.primitives.serialization.PublicFormat.SubjectPublicKeyInfo)
        try:
            hmac_token = jwt.encode({"iss": self.issuer, "sub": "x", "aud": AUDIENCE, "exp": int(time.time()) + 300},
                                    public_pem, algorithm="HS256", headers={"kid": "k1"})
        except jwt.InvalidKeyError:
            return  # the library itself refuses to build the attack
        self.refused(b, hmac_token, 401)

    def test_scope_is_required_when_configured(self):
        b = self.bridge(A2A_OIDC_SCOPE="a2a")
        self.auth(b, self.token())
        error = self.refused(b, self.token(scope="openid"), 403)
        self.assertEqual(error.error, "insufficient_scope")

    def test_allowed_subjects_limit_who_may_call(self):
        b = self.bridge(A2A_OIDC_ALLOWED_SUBJECTS="svc-jarvis, svc-herald")
        self.auth(b, self.token(sub="hash-1", preferred_username="svc-jarvis"))
        self.auth(b, self.token(sub="svc-herald"))
        error = self.refused(b, self.token(sub="hash-2", preferred_username="svc-other", azp="someone-else"), 403)
        self.assertEqual(error.error, "access_denied")

    def test_no_allowlist_means_any_valid_caller(self):
        self.auth(self.bridge(), self.token(sub="anyone"))

    def test_audience_may_be_a_list_of_clients(self):
        b = self.bridge(A2A_OIDC_AUDIENCE="other-client, " + AUDIENCE)
        self.auth(b, self.token())

    def test_static_token_still_works_next_to_oidc(self):
        b = self.bridge(A2A_TOKEN="s3cret")
        self.assertEqual(b.authenticate("Bearer s3cret"), "static-token")
        self.auth(b, self.token())
        with self.assertRaises(bridge.AuthError):
            b.authenticate("Bearer wrong")
        with self.assertRaises(bridge.AuthError):
            b.authenticate(None)

    def test_identity_provider_down_is_503(self):
        b = self.bridge(A2A_OIDC_ISSUER="http://127.0.0.1:9/application/o/a2a/")
        self.refused(b, self.token(iss="http://127.0.0.1:9/application/o/a2a/"), 503)

    def test_agent_card_announces_openid_connect(self):
        card = self.bridge(A2A_OIDC_SCOPE="a2a", A2A_TOKEN="s3cret").agent_card()
        self.assertEqual(
            card["securitySchemes"]["oidc"]["openIdConnectSecurityScheme"]["openIdConnectUrl"],
            self.issuer + ".well-known/openid-configuration")
        self.assertEqual(card["securityRequirements"][0], {"schemes": {"oidc": {"list": ["a2a"]}}})
        self.assertEqual(len(card["securityRequirements"]), 2)

    def test_http_statuses(self):
        b = self.bridge(A2A_OIDC_SCOPE="a2a")
        server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.make_handler(b))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def post(token):
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ListTasks"}).encode()
            try:
                with urllib.request.urlopen(urllib.request.Request(
                        f"http://127.0.0.1:{server.server_port}/rpc", body, headers)) as r:
                    return r.status, None
            except urllib.error.HTTPError as error:
                error.close()
                return error.code, error.headers.get("WWW-Authenticate")

        self.assertEqual(post(self.token())[0], 200)
        status, header = post(None)
        self.assertEqual(status, 401)
        self.assertNotIn("error=", header)
        status, header = post(self.token(iss="https://evil.example/"))
        self.assertEqual((status, 'error="invalid_token"' in header), (401, True))
        status, header = post(self.token(scope="openid"))
        self.assertEqual((status, 'error="insufficient_scope"' in header), (403, True))


if __name__ == "__main__":
    unittest.main()
