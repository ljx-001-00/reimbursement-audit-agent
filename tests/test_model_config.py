"""Exercise configurable model requests against a loopback-only test server."""
import json
import tempfile
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from agent import call_model, run_audit
from model_settings import DEFAULT_PROFILE, is_local_endpoint, load_profiles, model_endpoint, save_profiles, validate_model
from tests.test_agent_security import tool
from tests.test_rules import example


@contextmanager
def model_server(responses):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
            status, headers, payload = responses.pop(0) if responses else (200, {}, {})
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)


def completion(message):
    return (200, {}, {"choices": [{"message": message}]})


def prepared_example():
    app, policy, _invoice = example()
    for document in app.documents:
        document.text = "已识别的测试单据"
    return app, policy


class ModelConfigTests(unittest.TestCase):
    def test_base_url_or_full_endpoint(self):
        for base, expected in (
            ("https://example.test/v1", "https://example.test/v1/chat/completions"),
            ("https://example.test/v1/", "https://example.test/v1/chat/completions"),
            ("https://example.test/api/chat/completions", "https://example.test/api/chat/completions"),
            ("http://localhost:11434/v1", "http://localhost:11434/v1/chat/completions"),
        ):
            with self.subTest(base=base):
                self.assertEqual(model_endpoint(base), expected)

    def test_unsafe_url_components_are_rejected(self):
        for value in (
            "", "file:///tmp/model", "ftp://example.test", "https://user:secret@example.test/v1",
            "https://example.test/v1?api_key=secret", "https://example.test/v1#secret",
            "https://example.test/v1\nheader", "https://example.test/v1\tvalue",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                model_endpoint(value)

    def test_only_loopback_is_treated_as_local(self):
        for endpoint in ("http://localhost:11434/v1", "http://127.0.0.1:8000/v1", "http://[::1]:8000/v1"):
            with self.subTest(endpoint=endpoint):
                self.assertTrue(is_local_endpoint(endpoint))
        for endpoint in ("http://192.168.1.1/v1", "http://example.test/v1", "http://localhost.example.test/v1"):
            with self.subTest(endpoint=endpoint):
                self.assertFalse(is_local_endpoint(endpoint))

    def test_model_identifier_is_trimmed_but_not_fixed(self):
        self.assertEqual(validate_model("  provider/custom-model:latest  "), "provider/custom-model:latest")
        for value in ("", "   ", "model\nsecret", "model\tsecret", "x" * 201):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_model(value)

    def test_custom_model_and_key_reach_the_requested_endpoint(self):
        with model_server([completion({"role": "assistant", "content": "ready"})]) as (base, requests):
            result = call_model([{"role": "user", "content": "审核"}], "test-only-secret",
                                base_url=base + "/custom/chat/completions", model="custom/model-v2")
        self.assertEqual(result["content"], "ready")
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request["path"], "/custom/chat/completions")
        self.assertEqual(request["headers"].get("Authorization"), "Bearer test-only-secret")
        payload = json.loads(request["body"])
        self.assertEqual(payload["model"], "custom/model-v2")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "审核"}])
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["max_tokens"], 2048)
        self.assertEqual({item["function"]["name"] for item in payload["tools"]},
                         {"extract_documents", "audit_rules", "generate_report"})
        self.assertNotIn("thinking", payload)
        self.assertNotIn("temperature", payload)
        self.assertNotIn("test-only-secret", request["body"].decode("utf-8"))

    def test_loopback_model_runs_rules_locally_and_explains_once(self):
        app, policy = prepared_example()
        replies = [completion({"role": "assistant", "content": "存在超期与缺附件，请补正。"})]
        with model_server(replies) as (base, requests):
            run_audit(app, policy, use_ai=True, base_url=base + "/v1", model="local/tool-model")
        # A loopback model gets one explanation request; deterministic tools stay local.
        self.assertEqual(len(requests), 1)
        payload = json.loads(requests[0]["body"])
        self.assertEqual(payload["tools"], [])
        self.assertTrue(all("Authorization" not in request["headers"] for request in requests))
        # Only a verdict summary is sent, never the recognised document text.
        self.assertNotIn("已识别的测试单据", requests[0]["body"].decode("utf-8"))
        self.assertEqual(app.status, "预审通过")
        self.assertIn("本地模型辅助审核", app.mode)
        self.assertNotIn("降级", app.mode)
        self.assertIn("存在超期", app.model_note)

    def test_local_note_with_invented_number_is_rewritten_once(self):
        app, policy = prepared_example()
        replies = [completion({"role": "assistant", "content": "报账金额不能超过888元。"}),
                   completion({"role": "assistant", "content": "存在超期与缺附件，请补正。"})]
        with model_server(replies) as (base, requests):
            run_audit(app, policy, use_ai=True, base_url=base + "/v1", model="local/tool-model")
        self.assertEqual(len(requests), 2)
        self.assertIn("存在超期", app.model_note)
        self.assertNotIn("888", app.model_note)
        self.assertTrue(any("重写" in line for line in app.events))

    def test_local_note_dropped_when_invented_numbers_persist(self):
        app, policy = prepared_example()
        replies = [completion({"role": "assistant", "content": "金额上限888元。"}) for _ in range(2)]
        with model_server(replies) as (base, requests):
            run_audit(app, policy, use_ai=True, base_url=base + "/v1", model="local/tool-model")
        self.assertEqual(len(requests), 2)
        self.assertEqual(app.model_note, "")
        self.assertTrue(app.checks)
        self.assertTrue(any("丢弃" in line for line in app.events))

    def test_remote_without_key_stays_local_without_a_request(self):
        app, policy = prepared_example()
        with patch("agent.call_model") as model:
            run_audit(app, policy, use_ai=True, base_url="https://example.test/v1", model="other-model")
        model.assert_not_called()
        self.assertIn("未配置", app.mode)
        self.assertTrue(app.checks)

    def test_remote_http_does_not_send_a_key(self):
        with patch("urllib.request.OpenerDirector.open") as opened:
            with self.assertRaises(ValueError):
                call_model([], "test-only-secret", base_url="http://example.test/v1", model="other-model")
        opened.assert_not_called()

    def test_http_and_response_errors_fall_back_without_exposing_secrets(self):
        secret = "test-server-secret-DO-NOT-SAVE"
        responses = [
            (401, {}, {"error": secret}),
            (200, {}, b"invalid-json-" + secret.encode()),
            (200, {}, {"choices": []}),
        ]
        for reply in responses:
            with self.subTest(status=reply[0], body=reply[2]):
                app, policy = prepared_example()
                with model_server([reply]) as (base, requests):
                    run_audit(app, policy, secret, True, base_url=base + "/v1", model="test-model")
                self.assertEqual(len(requests), 1)
                self.assertIn("规则结论不受影响", app.mode)
                self.assertEqual(app.status, "预审通过")
                self.assertNotIn(secret, json.dumps(app.to_dict(), ensure_ascii=False))

    def test_redirect_does_not_forward_the_key_to_another_origin(self):
        secret = "redirect-test-secret"
        with model_server([]) as (target, target_requests):
            redirect = (302, {"Location": target + "/stolen"}, {})
            with model_server([redirect]) as (base, requests):
                with self.assertRaises(Exception):
                    call_model([], secret, base_url=base + "/v1", model="test-model")
        self.assertEqual(len(requests), 1)
        self.assertEqual(target_requests, [])

    def test_profiles_round_trip_only_nonsecret_fields(self):
        secret = "PROFILE_KEY_MUST_NOT_BE_SAVED"
        profiles = [dict(DEFAULT_PROFILE), {"name": "本机测试", "base_url": "http://localhost:11434/v1/",
                     "model": "local/test-model", "api_key": secret, "headers": {"Authorization": secret}}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings" / "models.json"
            with patch("model_settings.profile_path", return_value=path):
                save_profiles(profiles, "本机测试")
                loaded, active = load_profiles()
            self.assertEqual(active, "本机测试")
            self.assertEqual(loaded, [dict(DEFAULT_PROFILE), {"name": "本机测试", "base_url": "http://localhost:11434/v1",
                                      "model": "local/test-model"}])
            saved = path.read_text(encoding="utf-8")
            self.assertNotIn(secret, saved)
            self.assertNotIn("api_key", saved)
            self.assertNotIn("Authorization", saved)
            self.assertFalse(path.with_suffix(".tmp").exists())

    def test_missing_or_bad_profile_file_uses_default(self):
        malformed = ["not json", "[]", '{"profiles": null}', '{"profiles": []}',
                     '{"profiles": [{}]}', json.dumps({"profiles": [dict(DEFAULT_PROFILE), dict(DEFAULT_PROFILE)]})]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            with patch("model_settings.profile_path", return_value=path):
                self.assertEqual(load_profiles(), ([dict(DEFAULT_PROFILE)], DEFAULT_PROFILE["name"]))
                for payload in malformed:
                    with self.subTest(payload=payload):
                        path.write_text(payload, encoding="utf-8")
                        self.assertEqual(load_profiles(), ([dict(DEFAULT_PROFILE)], DEFAULT_PROFILE["name"]))

    def test_invalid_profile_save_preserves_previous_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.json"
            with patch("model_settings.profile_path", return_value=path):
                save_profiles([dict(DEFAULT_PROFILE)], DEFAULT_PROFILE["name"])
                before = path.read_bytes()
                for profiles, active in (([], "missing"), ([dict(DEFAULT_PROFILE)], "missing"),
                                         ([dict(DEFAULT_PROFILE), dict(DEFAULT_PROFILE)], DEFAULT_PROFILE["name"]),
                                         ([{**DEFAULT_PROFILE, "base_url": "https://example.test?key=secret"}], DEFAULT_PROFILE["name"])):
                    with self.subTest(profiles=profiles, active=active), self.assertRaises(ValueError):
                        save_profiles(profiles, active)
                    self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
