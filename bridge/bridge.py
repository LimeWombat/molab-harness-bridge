"""Translate OpenAI conversations into marimo's Vercel AI message format."""

import argparse
import json
import hmac
import mimetypes
import os
import re
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


MODEL_PROMPTS = {
    "wandb/zai-org/GLM-5.2": "glm-5.2.md",
    "wandb/zai-org/GLM-5.3-Flash": "glm-5-3-flash.md",
    "wandb/deepseek-ai/DeepSeek-V4.1-Flash": "deepseek-4-1.md",
    "wandb/deepseek-ai/DeepSeek-V4-Pro-0813": "deepseek v4.md",
    "wandb/deepseek-ai/DeepSeek-V4-Flash-0731": "deepseek v4.md",
    "wandb/deepseek-ai/DeepSeek-V4-Flash": "deepseek v4.md",
    "wandb/deepseek-ai/DeepSeek-V4-Pro": "deepseek v4.md",
    "wandb/Qwen/Qwen3.8-27B": "qwen-3.8.md",
    "wandb/reflection/Beam-1.0-501B-A23B": "Beam.md",
}


def to_ui_messages(messages):
    result = []
    pending = {}
    for message in messages:
        role = message["role"]
        if role == "tool":
            call_id = message["tool_call_id"]
            if call_id not in pending:
                raise ValueError("Tool result has no matching tool call")
            part = pending.pop(call_id)
            part["state"] = "output-available"
            part["output"] = message.get("content", "")
            continue
        if role == "developer":
            role = "system"
        parts = []
        if role == "assistant" and message.get("reasoning_content"):
            parts.append({"type": "reasoning", "text": message["reasoning_content"]})
        content = message.get("content")
        if isinstance(content, str):
            if content:
                parts.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for item in content:
                if item.get("type") == "text":
                    parts.append({"type": "text", "text": item["text"]})
                elif item.get("type") == "image_url":
                    url = item["image_url"]["url"]
                    media_type = url[5:].split(";", 1)[0].split(",", 1)[0] if url.startswith("data:") else mimetypes.guess_type(urllib.parse.urlparse(url).path)[0]
                    if not media_type or not media_type.startswith("image/"):
                        raise ValueError("Unsupported image type")
                    parts.append({"type": "file", "mediaType": media_type, "url": url})
                else:
                    raise ValueError("Unsupported content type")
        elif content is not None:
            raise ValueError("Unsupported message content")
        for call in message.get("tool_calls", []):
            function = call["function"]
            part = {
                "type": "tool-" + function["name"],
                "toolCallId": call["id"],
                "state": "input-available",
                "input": json.loads(function["arguments"]),
            }
            parts.append(part)
            pending[call["id"]] = part
        result.append({"id": uuid.uuid4().hex, "role": role, "parts": parts})
    return result


def to_marimo_request(request, prompt_dir=None):
    tools = []
    choice = request.get("tool_choice", "auto")
    if choice not in ("auto", "none", None):
        raise ValueError("Unsupported tool_choice; use auto or none")
    for tool in ([] if choice == "none" else request.get("tools", [])):
        if tool.get("type") != "function":
            raise ValueError("Only function tools are supported")
        function = tool["function"]
        tools.append({"name": function["name"], "description": function.get("description", ""),
                      "parameters": function.get("parameters", {"type": "object"}),
                      "source": "frontend", "mode": ["manual", "ask", "agent"]})
    messages = list(request["messages"])
    if prompt_dir is not None:
        prompt = Path(prompt_dir, MODEL_PROMPTS.get(request["model"], "Unified.md")).read_text(encoding="utf-8")
        messages.insert(0, {"role": "system", "content": prompt})
    return {"model": request["model"], "includeOtherCode": "", "uiMessages": to_ui_messages(messages),
            "id": uuid.uuid4().hex, "tools": tools, "options": {"webSearch": False}}


def response_from_events(events, model):
    text = []
    reasoning = []
    calls = []
    finish = "stop"
    finished = False
    for event in events:
        kind = event.get("type")
        if kind == "text-delta":
            text.append(event.get("delta", ""))
        elif kind == "reasoning-delta":
            reasoning.append(event.get("delta", ""))
        elif kind == "tool-input-available":
            calls.append({"id": event["toolCallId"], "type": "function", "function": {
                "name": event["toolName"], "arguments": json.dumps(event.get("input", {}), ensure_ascii=False)}})
        elif kind == "error":
            raise RuntimeError(event.get("errorText", "Upstream error"))
        elif kind == "finish":
            finished = True
            if event.get("finishReason") == "length":
                finish = "length"
    if not finished:
        raise RuntimeError("Upstream stream ended without finish")
    message = {"role": "assistant", "content": "".join(text) or None}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if calls:
        message["tool_calls"] = calls
        finish = "tool_calls"
    return {"id": "chatcmpl-" + uuid.uuid4().hex, "object": "chat.completion", "created": int(time.time()),
            "model": model, "choices": [{"index": 0, "message": message, "finish_reason": finish}]}


def stream_openai_events(events, model):
    response_id = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())

    def chunk(delta, finish=None):
        return {"id": response_id, "object": "chat.completion.chunk", "created": created,
                "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    yield chunk({"role": "assistant"})
    call_count = 0
    finish = "stop"
    finished = False
    for event in events:
        kind = event.get("type")
        if kind == "text-delta":
            yield chunk({"content": event.get("delta", "")})
        elif kind == "reasoning-delta":
            yield chunk({"reasoning_content": event.get("delta", "")})
        elif kind == "tool-input-available":
            yield chunk({"tool_calls": [{"index": call_count, "id": event["toolCallId"], "type": "function",
                                         "function": {"name": event["toolName"], "arguments": json.dumps(event.get("input", {}), ensure_ascii=False)}}]})
            call_count += 1
        elif kind == "error":
            raise RuntimeError(event.get("errorText", "Upstream error"))
        elif kind == "finish":
            finished = True
            if event.get("finishReason") == "length":
                finish = "length"
    if not finished:
        raise RuntimeError("Upstream stream ended without finish")
    yield chunk({}, "tool_calls" if call_count else finish)


def parse_sse(stream):
    data = []
    for raw in stream:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                value = "\n".join(data)
                data.clear()
                if value == "[DONE]":
                    return
                yield json.loads(value)
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))


def upstream_error_message(error):
    text = str(error)
    if re.search(r"status_code:\s*429\b", text):
        return "Molab rate limit reached; try again later"
    if re.search(r"status_code:\s*404\b", text):
        return "This model is unavailable from Molab (404)"
    return "Molab request failed"


def validate_upstream(config):
    upstream_url = urllib.parse.urlparse(config["base_url"])
    if upstream_url.username or upstream_url.password or upstream_url.query or upstream_url.fragment or not (
        (upstream_url.scheme == "https" and (upstream_url.hostname or "").endswith(".sb.molab.run"))
        or (upstream_url.scheme == "http" and upstream_url.hostname in ("127.0.0.1", "::1"))
    ):
        raise ValueError("Invalid upstream address")


def parse_pair_prompt(prompt):
    match = re.search(r"Connect to the notebook at:\s*(https://[^\s`]+)", prompt)
    token = re.search(r"--token\s+(?:'([A-Za-z0-9._~-]+)'|\"([A-Za-z0-9._~-]+)\"|([A-Za-z0-9._~-]+)(?=[\s`]|$))", prompt)
    if not match or not token:
        raise ValueError("Pair prompt is missing an address or an unmasked token")
    base_url = match.group(1).rstrip("/")
    config = {"base_url": base_url, "token": next(value for value in token.groups() if value is not None)}
    validate_upstream(config)
    for referenced in re.findall(r"--url\s+(https://[^\s`]+)", prompt):
        if referenced.rstrip("/") != base_url:
            raise ValueError("Pair prompt contains inconsistent upstream addresses")
    return config


def is_windows():
    return sys.platform == "win32"


def _windows_dpapi(data, decrypt=False):
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    incoming, outgoing = Blob(len(data), buffer), Blob()
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel.LocalFree(ctypes.cast(outgoing.data, ctypes.c_void_p))


def protect_windows(data):
    return _windows_dpapi(data)


def unprotect_windows(data):
    return _windows_dpapi(data, decrypt=True)


def private_file(path):
    if path.is_symlink() or (not is_windows() and path.stat().st_mode & 0o077):
        raise ValueError("Session file is not private")


def write_session(path, session):
    validate_upstream(session)
    path = Path(path)
    existing = {}
    if path.exists():
        existing = read_session(path)
    data = dict(session)
    data["local_key"] = existing.get("local_key") or secrets.token_urlsafe(32)
    contents = json.dumps(data).encode("utf-8")
    if is_windows():
        contents = b"DPAPI1\n" + protect_windows(contents)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".session-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(contents)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_session(path):
    path = Path(path)
    private_file(path)
    contents = path.read_bytes()
    if is_windows():
        if not contents.startswith(b"DPAPI1\n"):
            raise ValueError("Session file is not encrypted")
        contents = unprotect_windows(contents[len(b"DPAPI1\n"):])
    data = json.loads(contents)
    validate_upstream(data)
    if not all(isinstance(data.get(k), str) and data[k] for k in ("token", "local_key")):
        raise ValueError("Session file is incomplete")
    return data


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def discover_session(config):
    validate_upstream(config)
    request = urllib.request.Request(config["base_url"].rstrip("/") + "/api/sessions", headers={
        "Authorization": "Bearer " + config["token"], "User-Agent": "molab-local-connector/1.0"})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
        sessions = json.load(response)
    if not isinstance(sessions, dict) or len(sessions) != 1:
        raise RuntimeError("Expected one active notebook session")
    return next(iter(sessions))


def make_server(host, port, config):
    if host not in ("127.0.0.1", "::1"):
        raise ValueError("The bridge must bind to loopback")
    validate_upstream(config)
    opener = urllib.request.build_opener(NoRedirect())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, status, obj):
            data = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            provided = self.headers.get("Authorization", "")
            return hmac.compare_digest(provided, "Bearer " + config["local_key"])

        def do_GET(self):
            if not self.authorized():
                return self.send_json(401, {"error": {"message": "Unauthorized"}})
            if self.path != "/v1/models":
                return self.send_json(404, {"error": {"message": "Not found"}})
            return self.send_json(200, {"object": "list", "data": [
                {"id": name, "object": "model", "owned_by": "molab"} for name in config["models"]]})

        def do_POST(self):
            if not self.authorized():
                return self.send_json(401, {"error": {"message": "Unauthorized"}})
            if self.path != "/v1/chat/completions":
                return self.send_json(404, {"error": {"message": "Not found"}})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 10_000_000:
                    raise ValueError("Invalid request size")
                body = json.loads(self.rfile.read(length))
                if body.get("model") not in config["models"]:
                    raise ValueError("Model is not available in this session")
                payload = json.dumps(to_marimo_request(body, config.get("prompt_dir"))).encode()
                current = {**config, **read_session(config["session_path"])} if config.get("session_path") else config
                if not hmac.compare_digest(current["local_key"], config["local_key"]):
                    raise ValueError("Local client key changed; restart the bridge")
                session_id = current.get("session_id") or discover_session(current)
                upstream = urllib.request.Request(current["base_url"].rstrip("/") + "/api/ai/chat", payload,
                    headers={"Content-Type": "application/json", "Accept": "text/event-stream",
                             "User-Agent": "molab-local-connector/1.0",
                             "Authorization": "Bearer " + current["token"], "Marimo-Session-Id": session_id})
                with opener.open(upstream, timeout=180) as response:
                    events = parse_sse(response)
                    if not body.get("stream", False):
                        return self.send_json(200, response_from_events(events, body["model"]))
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    try:
                        for chunk in stream_openai_events(events, body["model"]):
                            self.wfile.write(("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode())
                            self.wfile.flush()
                        self.wfile.write(b"data: [DONE]\n\n")
                    except (ValueError, RuntimeError) as error:
                        self.wfile.write(("data: " + json.dumps({"error": {"message": upstream_error_message(error)}}) + "\n\n").encode())
                    return
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
                return self.send_json(400, {"error": {"message": str(error)}})
            except urllib.error.HTTPError as error:
                return self.send_json(502, {"error": {"message": f"Molab returned HTTP {error.code}"}})
            except RuntimeError as error:
                return self.send_json(502, {"error": {"message": upstream_error_message(error)}})
            except (urllib.error.URLError, TimeoutError) as error:
                return self.send_json(502, {"error": {"message": f"Molab request failed: {type(error).__name__}"}})
            except OSError:
                return self.send_json(503, {"error": {"message": "Molab session file is unavailable"}})

    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description="Local OpenAI-compatible bridge for a running Molab notebook")
    parser.add_argument("action", choices=("import-prompt", "check", "key", "serve"))
    parser.add_argument("--session", default=str(Path(__file__).with_name("session.json")))
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if args.action == "import-prompt":
        write_session(args.session, parse_pair_prompt(sys.stdin.read()))
        print("Molab session saved securely")
        return
    config = read_session(args.session)
    if args.action == "key":
        print(config["local_key"])
        return
    if args.action == "check":
        discover_session(config)
        print("Molab notebook is reachable")
        return
    catalog_path = Path(__file__).with_name("models.json")
    config["models"] = [model["id"] for model in json.loads(catalog_path.read_text(encoding="utf-8"))]
    config["prompt_dir"] = str(Path(__file__).parent.parent / "jailbreaks")
    config["session_path"] = args.session
    server = make_server("127.0.0.1", args.port, config)
    print(f"Listening on http://127.0.0.1:{server.server_port}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
