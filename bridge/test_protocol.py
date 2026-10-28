import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import threading
import tempfile
import urllib.request
import urllib.error
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch


def load_bridge():
    spec = importlib.util.find_spec("bridge")
    if spec is None:
        raise AssertionError("bridge module is not implemented")
    return importlib.import_module("bridge")


class ProtocolTest(unittest.TestCase):
    def test_gateway_does_not_add_jailbreak_to_client_messages(self):
        bridge = load_bridge()
        captured = []
        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                captured.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                self.send_response(200); self.end_headers()
                self.wfile.write(b'data: {"type":"text-delta","delta":"ok"}\n\ndata: {"type":"finish"}\n\n')
            def log_message(self, *args):
                pass
        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'Unified.md').write_text('unwanted hidden instruction')
            server = bridge.make_server('127.0.0.1', 0, {'base_url':f'http://127.0.0.1:{upstream.server_port}',
                'token':'test', 'session_id':'test', 'local_key':'test', 'models':['m'], 'prompt_dir':directory})
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                messages = [{'role':'system','content':'client instruction'}, {'role':'user','content':'hello'}]
                request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/chat/completions',
                    json.dumps({'model':'m', 'messages':messages}).encode(),
                    {'Authorization':'Bearer test', 'Content-Type':'application/json'})
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(json.load(response)['choices'][0]['message']['content'], 'ok')
                self.assertEqual([m['parts'][0]['text'] for m in captured[0]['uiMessages']], ['client instruction', 'hello'])
            finally:
                server.shutdown(); server.server_close(); upstream.shutdown(); upstream.server_close()

    def test_client_key_command_reads_the_private_session(self):
        bridge = load_bridge()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, 'session.json')
            bridge.write_session(target, {'base_url': 'https://sb-test.sb.molab.run', 'token': 'private'})
            expected = bridge.read_session(target)['local_key']
            result = subprocess.run([sys.executable, bridge.__file__, 'key', '--session', str(target)],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), expected)

    def test_windows_session_is_encrypted_and_refresh_preserves_client_key(self):
        bridge = load_bridge()
        def encrypt(value):
            return value[::-1]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(bridge, 'is_windows', return_value=True), \
             patch.object(bridge, 'protect_windows', side_effect=encrypt), \
             patch.object(bridge, 'unprotect_windows', side_effect=encrypt):
            target = Path(directory, 'session.json')
            bridge.write_session(target, {'base_url': 'https://sb-test.sb.molab.run', 'token': 'private-token'})
            first = bridge.read_session(target)
            self.assertNotIn(b'private-token', target.read_bytes())
            bridge.write_session(target, {'base_url': 'https://sb-test.sb.molab.run', 'token': 'new-token'})
            second = bridge.read_session(target)
            self.assertEqual(first['local_key'], second['local_key'])
            self.assertEqual(second['token'], 'new-token')

    def test_windows_rejects_unencrypted_session(self):
        bridge = load_bridge()
        with tempfile.TemporaryDirectory() as directory, patch.object(bridge, 'is_windows', return_value=True):
            target = Path(directory, 'session.json')
            target.write_text('{"token":"plain"}')
            with self.assertRaisesRegex(ValueError, 'not encrypted'):
                bridge.read_session(target)

    @unittest.skipUnless(sys.platform == 'win32', 'requires Windows DPAPI')
    def test_windows_dpapi_roundtrip(self):
        bridge = load_bridge()
        secret = b'private-session-token'
        encrypted = bridge.protect_windows(secret)
        self.assertNotIn(secret, encrypted)
        self.assertEqual(bridge.unprotect_windows(encrypted), secret)

    def test_upstream_error_explains_rate_limit_and_unavailable_model(self):
        bridge = load_bridge()
        self.assertIn('rate limit', bridge.upstream_error_message(RuntimeError('status_code: 429 secret')))
        self.assertIn('unavailable', bridge.upstream_error_message(RuntimeError('status_code: 404 secret')))
        self.assertNotIn('secret', bridge.upstream_error_message(RuntimeError('status_code: 429 secret')))

    def test_tool_result_is_preserved_with_assistant_call(self):
        bridge = load_bridge()
        messages = [
            {"role": "user", "content": "Read the file"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"x"}'}}
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "hello"},
        ]
        result = bridge.to_ui_messages(messages)
        part = result[-1]["parts"][0]
        self.assertEqual(part["toolCallId"], "call_1")
        self.assertEqual(part["input"], {"path": "x"})
        self.assertEqual(part["output"], "hello")
        self.assertEqual(part["state"], "output-available")

    def test_external_tools_are_deferred_for_client(self):
        bridge = load_bridge()
        payload = bridge.to_marimo_request({
            "model": "wandb/zai-org/GLM-5.2",
            "messages": [{"role": "user", "content": "Read the file"}],
            "tools": [{"type": "function", "function": {
                "name": "read_file", "description": "Read a file", "parameters": {"type": "object"}
            }}],
        })
        self.assertEqual(payload["tools"][0]["source"], "frontend")
        self.assertEqual(payload["tools"][0]["parameters"], {"type": "object"})
        self.assertEqual(payload["model"], "wandb/zai-org/GLM-5.2")

    def test_tool_choice_none_does_not_expose_tools(self):
        bridge = load_bridge()
        request = {"model": "m", "messages": [{"role": "user", "content": "hello"}], "tool_choice": "none",
                   "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]}
        self.assertEqual(bridge.to_marimo_request(request)["tools"], [])

    def test_required_tool_choice_is_not_silently_ignored(self):
        bridge = load_bridge()
        request = {"model": "m", "messages": [{"role": "user", "content": "hello"}], "tool_choice": "required"}
        with self.assertRaisesRegex(ValueError, "tool_choice"):
            bridge.to_marimo_request(request)

    def test_text_and_tool_calls_from_stream(self):
        bridge = load_bridge()
        events = [
            {"type": "text-delta", "delta": "Reading."},
            {"type": "tool-input-available", "toolCallId": "call_1", "toolName": "read_file", "input": {"path": "x"}},
            {"type": "finish", "finishReason": "tool-calls"},
        ]
        reply = bridge.response_from_events(events, "model")
        self.assertEqual(reply["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(reply["choices"][0]["message"]["content"], "Reading.")
        self.assertEqual(reply["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "read_file")

    def test_reasoning_is_not_lost_in_non_stream_response(self):
        bridge = load_bridge()
        events = [{"type": "reasoning-delta", "delta": "think"},
                  {"type": "text-delta", "delta": "done"},
                  {"type": "finish", "finishReason": "stop"}]
        reply = bridge.response_from_events(events, "model")
        self.assertEqual(reply["choices"][0]["message"]["reasoning_content"], "think")

    def test_assistant_reasoning_is_preserved_in_followup(self):
        bridge = load_bridge()
        result = bridge.to_ui_messages([{"role": "assistant", "content": "answer", "reasoning_content": "thinking"}])
        self.assertTrue(any(p.get("type") == "reasoning" and p.get("text") == "thinking" for p in result[0]["parts"]))

    def test_stream_exposes_text_and_tool_call_with_finish_reason(self):
        bridge = load_bridge()
        events = [
            {"type": "text-delta", "delta": "One"},
            {"type": "text-delta", "delta": " two"},
            {"type": "tool-input-available", "toolCallId": "c1", "toolName": "read_file", "input": {"path": "a"}},
            {"type": "finish", "finishReason": "tool-calls"},
        ]
        chunks = list(bridge.stream_openai_events(events, "model"))
        self.assertEqual("".join(c["choices"][0]["delta"].get("content", "") for c in chunks), "One two")
        tool = next(c["choices"][0]["delta"]["tool_calls"][0] for c in chunks if "tool_calls" in c["choices"][0]["delta"])
        self.assertEqual(tool["id"], "c1")
        self.assertEqual(tool["function"]["name"], "read_file")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "tool_calls")

    def test_sse_parses_heartbeats_and_utf8(self):
        bridge = load_bridge()
        data = ': keepalive\n\ndata: {"type":"text-delta","delta":"Привет"}\n\ndata: [DONE]\n\n'.encode()
        self.assertEqual(list(bridge.parse_sse(io.BytesIO(data))), [{"type": "text-delta", "delta": "Привет"}])

    def test_stream_fails_if_upstream_drops_before_finish(self):
        bridge = load_bridge()
        with self.assertRaisesRegex(RuntimeError, "finish"):
            list(bridge.stream_openai_events([{"type": "text-delta", "delta": "partial"}], "model"))

    def test_http_gateway_transmits_tool_and_returns_model_response(self):
        bridge = load_bridge()
        captured = []
        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                captured.append({"body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                                 "agent": self.headers.get("User-Agent")})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for event in [
                    {"type": "tool-input-available", "toolCallId": "call_1", "toolName": "read_file", "input": {"path": "x"}},
                    {"type": "finish", "finishReason": "tool-calls"},
                ]:
                    self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            def log_message(self, *args):
                pass
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        server = bridge.make_server("127.0.0.1", 0, {
            "base_url": f"http://127.0.0.1:{upstream.server_port}", "token": "upstream-test",
            "session_id": "session-test", "local_key": "local-test", "models": ["wandb/zai-org/GLM-5.2"]
        })
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            payload = {"model": "wandb/zai-org/GLM-5.2", "messages": [{"role": "user", "content": "read x"}],
                       "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}]}
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                json.dumps(payload).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer local-test"})
            with urllib.request.urlopen(request) as response:
                result = json.load(response)
            self.assertEqual(result["choices"][0]["message"]["tool_calls"][0]["id"], "call_1")
            self.assertEqual(captured[0]["body"]["tools"][0]["source"], "frontend")
            self.assertEqual(captured[0]["agent"], "molab-local-connector/1.0")
        finally:
            server.shutdown()
            server.server_close()
            upstream.shutdown()
            upstream.server_close()

    def test_gateway_stream_returns_standard_sse(self):
        bridge = load_bridge()
        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"type":"text-delta","delta":"ok"}\n\ndata: {"type":"finish","finishReason":"stop"}\n\ndata: [DONE]\n\n')
            def log_message(self, *args):
                pass
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        server = bridge.make_server("127.0.0.1", 0, {"base_url": f"http://127.0.0.1:{upstream.server_port}",
            "token": "upstream-test", "session_id": "session-test", "local_key": "local-test", "models": ["m"]})
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            payload = {"model": "m", "messages": [{"role": "user", "content": "hello"}], "stream": True}
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                json.dumps(payload).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer local-test"})
            with urllib.request.urlopen(request) as response:
                chunks = list(bridge.parse_sse(response))
            self.assertEqual(chunks[1]["choices"][0]["delta"]["content"], "ok")
            self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
        finally:
            server.shutdown()
            server.server_close()
            upstream.shutdown()
            upstream.server_close()

    def test_converts_image_input_into_vercel_file_part(self):
        bridge = load_bridge()
        result = bridge.to_ui_messages([{"role": "user", "content": [
            {"type": "text", "text": "Describe this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,YWJj"}},
        ]}])
        self.assertEqual(result[0]["parts"], [
            {"type": "text", "text": "Describe this"},
            {"type": "file", "mediaType": "image/png", "url": "data:image/png;base64,YWJj"},
        ])

    def test_unknown_tool_result_is_rejected(self):
        bridge = load_bridge()
        with self.assertRaisesRegex(ValueError, "tool call"):
            bridge.to_ui_messages([{"role": "tool", "tool_call_id": "missing", "content": "x"}])

    def test_unauthorized_client_cannot_list_models(self):
        bridge = load_bridge()
        server = bridge.make_server("127.0.0.1", 0, {
            "base_url": "http://127.0.0.1:1", "token": "x", "session_id": "x", "local_key": "secret", "models": ["m"]
        })
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/v1/models")
            self.assertEqual(error.exception.code, 401)
        finally:
            server.shutdown()
            server.server_close()

    def test_gateway_will_not_bind_external_interfaces(self):
        bridge = load_bridge()
        with self.assertRaisesRegex(ValueError, "loopback"):
            bridge.make_server("0.0.0.0", 0, {})

    def test_gateway_will_not_send_molab_credentials_to_external_host(self):
        bridge = load_bridge()
        with self.assertRaisesRegex(ValueError, "upstream"):
            bridge.make_server("127.0.0.1", 0, {"base_url": "https://example.com"})

    def test_empty_assistant_content_with_tool_call(self):
        bridge = load_bridge()
        result = bridge.to_ui_messages([{"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
        ]}])
        self.assertEqual(result[0]["parts"][0]["toolCallId"], "c1")

    def test_two_turn_tool_roundtrip_through_gateway(self):
        bridge = load_bridge()
        seen = []
        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append(body)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if len(seen) == 1:
                    event = {"type": "tool-input-available", "toolCallId": "call_1", "toolName": "read_file", "input": {"path": "example.txt"}}
                    finish = "tool-calls"
                else:
                    event = {"type": "text-delta", "delta": "File contents: example"}
                    finish = "stop"
                for item in (event, {"type": "finish", "finishReason": finish}):
                    self.wfile.write(("data: " + json.dumps(item) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            def log_message(self, *args):
                pass
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        server = bridge.make_server("127.0.0.1", 0, {"base_url": f"http://127.0.0.1:{upstream.server_port}",
            "token": "test", "session_id": "test", "local_key": "local-test", "models": ["m"]})
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            address = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
            messages = [{"role": "user", "content": "Read example.txt"}]
            tool = {"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}
            def send():
                request = urllib.request.Request(address, json.dumps({"model": "m", "messages": messages, "tools": [tool]}).encode(),
                    headers={"Authorization": "Bearer local-test", "Content-Type": "application/json"})
                with urllib.request.urlopen(request) as response:
                    return json.load(response)["choices"][0]["message"]
            messages.append(send())
            messages.append({"role": "tool", "tool_call_id": "call_1", "content": "example"})
            final = send()
            self.assertEqual(final["content"], "File contents: example")
            self.assertEqual(seen[1]["uiMessages"][-1]["parts"][0]["output"], "example")
        finally:
            server.shutdown(); server.server_close()
            upstream.shutdown(); upstream.server_close()

    def test_discovers_single_active_session(self):
        bridge = load_bridge()
        class Sessions(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"live-session":{"path":"notebook.py"}}')
            def log_message(self, *args):
                pass
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Sessions)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        try:
            config = {"base_url": f"http://127.0.0.1:{upstream.server_port}", "token": "test"}
            self.assertEqual(bridge.discover_session(config), "live-session")
        finally:
            upstream.shutdown(); upstream.server_close()

    def test_pair_prompt_can_be_imported_without_storing_shell_command(self):
        bridge = load_bridge()
        prompt = """Connect to the notebook at: https://sb-test-123.sb.molab.run/
Use this auth token: `execute-code.sh --url https://sb-test-123.sb.molab.run/ --token 'sample-token'`.
"""
        config = bridge.parse_pair_prompt(prompt)
        self.assertEqual(config, {"base_url": "https://sb-test-123.sb.molab.run", "token": "sample-token"})

    def test_current_molab_pair_prompt_accepts_unquoted_token(self):
        bridge = load_bridge()
        prompt = "Connect to the notebook at: https://sb-test.sb.molab.run/\nUse `execute-code.sh --url https://sb-test.sb.molab.run/ --token sample-token`."
        self.assertEqual(bridge.parse_pair_prompt(prompt)["token"], "sample-token")

    def test_pair_prompt_rejects_foreign_or_inconsistent_targets(self):
        bridge = load_bridge()
        with self.assertRaisesRegex(ValueError, "upstream"):
            bridge.parse_pair_prompt("Connect to the notebook at: https://evil.example/\n--token 'secret'")

    def test_session_configuration_is_private(self):
        bridge = load_bridge()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "session.json")
            bridge.write_session(target, {"base_url": "https://sb-test.sb.molab.run", "token": "private"})
            if not bridge.is_windows():
                self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
            self.assertEqual(bridge.read_session(target)["token"], "private")

    def test_session_refresh_preserves_local_client_key(self):
        bridge = load_bridge()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "session.json")
            bridge.write_session(target, {"base_url": "https://sb-test.sb.molab.run", "token": "first"})
            old = bridge.read_session(target)
            bridge.write_session(target, {"base_url": "https://sb-new.sb.molab.run", "token": "second"})
            new = bridge.read_session(target)
            self.assertEqual(old["local_key"], new["local_key"])
            self.assertEqual(new["token"], "second")

    @unittest.skipIf(sys.platform == 'win32', 'Windows uses DPAPI instead of Unix permissions')
    def test_session_loading_rejects_world_readable_credentials(self):
        bridge = load_bridge()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "session.json")
            target.write_text(json.dumps({"base_url": "https://sb-test.sb.molab.run", "token": "private", "local_key": "local"}))
            target.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "private"):
                bridge.read_session(target)

    def test_running_gateway_uses_refreshed_session(self):
        bridge = load_bridge()
        tokens = []
        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                tokens.append(self.headers['Authorization'])
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'data: {"type":"text-delta","delta":"ok"}\n\ndata: {"type":"finish"}\n\n')
            def log_message(self, *args):
                pass
        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, 'session.json')
            url = f'http://127.0.0.1:{upstream.server_port}'
            bridge.write_session(target, {'base_url': url, 'token': 'first'})
            config = {**bridge.read_session(target), 'session_path': str(target), 'session_id': 'test', 'models': ['m']}
            server = bridge.make_server('127.0.0.1', 0, config)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                bridge.write_session(target, {'base_url': url, 'token': 'second'})
                request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/chat/completions',
                    json.dumps({'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]}).encode(),
                    headers={'Authorization': 'Bearer ' + config['local_key'], 'Content-Type': 'application/json'})
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(json.load(response)['choices'][0]['message']['content'], 'ok')
                self.assertEqual(tokens, ['Bearer second'])
            finally:
                server.shutdown(); server.server_close()
                upstream.shutdown(); upstream.server_close()


if __name__ == "__main__":
    unittest.main()
