#!/usr/bin/env python3
"""A2A bridge to the inbox of container-agent-claude.

Speaks the Agent2Agent protocol (v1.0, JSON-RPC binding) and turns every
message into a file of the agent's inbox. The agent answers at the end of the
file and moves it to inbox/done/: that move is what completes the A2A task.

Task state lives entirely on disk, so the bridge itself is stateless:

    inbox/a2a-<id>.md        task submitted or being worked on (agent.log tells which)
    inbox/done/a2a-<id>.md   task completed (or failed if nothing was answered)
    state/a2a/canceled/<id>.json   task canceled (a cancel file is also left in the inbox)
"""

import hmac
import json
import logging
import os
import re
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = os.environ.get("A2A_BRIDGE_VERSION", "dev")
PROTOCOL_VERSION = "1.0"
DOMAIN = "a2a-protocol.org"

HEADER = "<!-- a2a task={task} context={context} created={created} -->"
HEADER_RE = re.compile(
    r"\A<!-- a2a task=(?P<task>[0-9a-f-]{36}) context=(?P<context>\S+) created=(?P<created>\S+) -->"
)
REQUEST_START = "<!-- a2a:request -->"
REQUEST_END = "<!-- a2a:request-end -->"
ID_RE = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
CONTEXT_RE = re.compile(r"\A[A-Za-z0-9._:-]{1,128}\Z")

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
COMPLETED = "TASK_STATE_COMPLETED"
FAILED = "TASK_STATE_FAILED"
CANCELED = "TASK_STATE_CANCELED"
TERMINAL = {COMPLETED, FAILED, CANCELED}

DEFAULT_PREAMBLE = (
    "This message arrived through the A2A bridge, sent by a remote agent or "
    "application. It was not typed by your human."
)

log = logging.getLogger("a2a-bridge")


class RpcError(Exception):
    """An error that maps to a JSON-RPC error object."""

    def __init__(self, code, message, reason=None, metadata=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason
        self.metadata = metadata or {}

    def to_json(self):
        error = {"code": self.code, "message": self.message}
        if self.reason:
            error["data"] = [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": self.reason,
                    "domain": DOMAIN,
                    "metadata": {k: str(v) for k, v in self.metadata.items()},
                }
            ]
        return error


def parse_error():
    return RpcError(-32700, "Invalid JSON payload")


def invalid_request(detail=None):
    return RpcError(-32600, detail or "Request payload validation error")


def invalid_params(detail):
    return RpcError(-32602, f"Invalid parameters: {detail}")


def task_not_found(task_id):
    return RpcError(-32001, "Task not found", "TASK_NOT_FOUND", {"taskId": task_id})


def now():
    return datetime.now(timezone.utc)


def iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def text_message(message_id, role, text, context_id, task_id):
    return {
        "messageId": message_id,
        "contextId": context_id,
        "taskId": task_id,
        "role": role,
        "parts": [{"text": text, "mediaType": "text/plain"}],
    }


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.data_dir = Path(env.get("A2A_DATA_DIR", "/agent/data"))
        self.port = int(env.get("A2A_PORT", "8080"))
        self.public_url = env.get("A2A_PUBLIC_URL", f"http://localhost:{self.port}").rstrip("/")
        self.token = env.get("A2A_TOKEN", "")
        self.name = env.get("A2A_NAME", "Claude agent")
        self.description = env.get(
            "A2A_DESCRIPTION",
            "An autonomous Claude Code agent. Messages are left in its inbox and "
            "answered in its next run, usually within a minute.",
        )
        self.block_timeout = float(env.get("A2A_BLOCK_TIMEOUT", "300"))
        self.poll_interval = float(env.get("A2A_POLL_INTERVAL", "1"))
        self.max_body = int(env.get("A2A_MAX_BODY", str(1024 * 1024)))
        self.max_pending = int(env.get("A2A_MAX_PENDING", "20"))
        self.preamble = env.get("A2A_PREAMBLE", DEFAULT_PREAMBLE)
        self.run_max = float(env.get("A2A_RUN_MAX", "7200"))
        self.oidc_issuer = env.get("A2A_OIDC_ISSUER", "").strip()
        self.oidc_audience = [a.strip() for a in env.get("A2A_OIDC_AUDIENCE", "").split(",") if a.strip()]
        self.oidc_scope = env.get("A2A_OIDC_SCOPE", "").strip()
        self.oidc_jwks_url = env.get("A2A_OIDC_JWKS_URL", "").strip()
        self.oidc_allowed_subjects = [s.strip() for s in env.get("A2A_OIDC_ALLOWED_SUBJECTS", "").split(",") if s.strip()]
        self.oidc_algorithms = [a.strip() for a in env.get("A2A_OIDC_ALGORITHMS", "RS256,ES256").split(",") if a.strip()]

    @property
    def inbox(self):
        return self.data_dir / "inbox"

    @property
    def done(self):
        return self.inbox / "done"

    @property
    def canceled(self):
        return self.data_dir / "state" / "a2a" / "canceled"


class Store:
    """Maps A2A tasks onto the files of the agent's inbox."""

    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        for folder in (config.inbox, config.done, config.canceled):
            folder.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def filename(task_id, cancel=False):
        return f"a2a-{task_id}{'-cancel' if cancel else ''}.md"

    @staticmethod
    def _answer_of(path):
        """The text written after the request marker of a file, or ''."""
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        end = raw.find(REQUEST_END)
        return raw[end + len(REQUEST_END) :].strip() if end >= 0 else ""

    def pending_count(self):
        return sum(1 for p in self.config.inbox.glob("a2a-*.md") if not p.name.endswith("-cancel.md"))

    # -- what the agent says about itself: logs/agent.log and the run transcripts

    def _runs(self):
        """Runs found at the end of agent.log, oldest first."""
        try:
            with (self.config.data_dir / "logs" / "agent.log").open("rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 131072))
                text = f.read().decode("utf-8", "replace")
        except OSError:
            return []
        runs = []
        for line in text.splitlines():
            stamp, _, message = line.partition(" ")
            try:
                moment = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if message.startswith("run started,"):
                runs.append({"start": moment, "end": None, "transcript": None})
            elif message.startswith("run ended with status") and runs and runs[-1]["end"] is None:
                runs[-1]["end"] = moment
                found = re.search(r"transcript (\S+)", message)
                runs[-1]["transcript"] = found[1] if found else None
        return runs

    def active_run(self):
        runs = self._runs()
        if runs and runs[-1]["end"] is None:
            age = (now() - runs[-1]["start"]).total_seconds()
            if age < self.config.run_max:  # a crashed container never logs the end
                return runs[-1]
        return None

    def run_at(self, moment):
        for run in reversed(self._runs()):
            if run["start"] <= moment and (run["end"] is None or moment <= run["end"].replace(microsecond=0) + timedelta(seconds=2)):
                return run
        return None

    @staticmethod
    def _tail_lines(path, size):
        try:
            with path.open("rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - size))
                return f.read().decode("utf-8", "replace").splitlines()[::-1]
        except OSError:
            return []

    def progress(self):
        """What the running agent last said or did, from its newest transcript."""
        runs = self.config.data_dir / "logs" / "runs"
        try:
            newest = max(runs.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        except (ValueError, OSError):
            return None
        for line in self._tail_lines(newest, 65536):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") != "assistant":
                continue
            blocks = (event.get("message") or {}).get("content") or []
            for block in reversed(blocks if isinstance(blocks, list) else []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    return block["text"].strip()[:300]
                if block.get("type") == "tool_use":
                    return f"Using tool {block.get('name', '?')}"
        return None

    def run_result(self, run):
        """The final text of a finished run, when the agent wrote no answer in the file."""
        if not run or not run["transcript"]:
            return None
        path = self.config.data_dir / "logs" / "runs" / Path(run["transcript"]).name
        for line in self._tail_lines(path, 262144):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "result":
                result = event.get("result")
                if event.get("is_error") or not isinstance(result, str) or not result.strip():
                    return None
                return result.strip()
        return None

    def create(self, text, context_id, caller=None):
        with self.lock:
            if self.pending_count() >= self.config.max_pending:
                raise RpcError(
                    -32603,
                    f"Too many tasks waiting for the agent (limit {self.config.max_pending})",
                    "TOO_MANY_PENDING",
                )
            task_id = str(uuid.uuid4())
            context_id = context_id or str(uuid.uuid4())
            text = text.replace("<!-- a2a", "<!--a2a")
            lines = [HEADER.format(task=task_id, context=context_id, created=iso(now()))]
            if self.config.preamble:
                lines.append(self.config.preamble)
            if caller:
                lines.append(f"Authenticated caller: {caller}.")
            lines += ["", REQUEST_START, text, REQUEST_END, ""]
            # A dot file is ignored by the agent: it only sees the finished one.
            temporary = self.config.inbox / f".{self.filename(task_id)}.tmp"
            temporary.write_text("\n".join(lines), encoding="utf-8")
            os.replace(temporary, self.config.inbox / self.filename(task_id))
            return task_id

    def locate(self, task_id):
        """Returns (path, state) or (None, None) when the task is unknown."""
        if not ID_RE.match(task_id):
            return None, None
        name = self.filename(task_id)
        if (self.config.canceled / f"{task_id}.json").exists():
            return self.config.canceled / f"{task_id}.json", CANCELED
        if (self.config.done / name).exists():
            return self.config.done / name, COMPLETED
        if (self.config.inbox / name).exists():
            return self.config.inbox / name, SUBMITTED
        return None, None

    def is_finished(self, task_id):
        try:
            return self.task(task_id, include_artifacts=False)["status"]["state"] in TERMINAL
        except RpcError:
            return True

    def task(self, task_id, history_length=0, include_artifacts=True):
        path, state = self.locate(task_id)
        if path is None:
            raise task_not_found(task_id)
        return self._build(path, state, history_length, include_artifacts)

    def _build(self, path, state, history_length, include_artifacts):
        if state == CANCELED:
            info = json.loads(path.read_text(encoding="utf-8"))
            task = {
                "id": info["id"],
                "contextId": info["contextId"],
                "status": {"state": CANCELED, "timestamp": info["canceled"]},
            }
            # What the agent said when it was told to stop, once it has said it.
            reply = self._answer_of(self.config.done / self.filename(info["id"], cancel=True))
            if reply:
                task["status"]["message"] = text_message(
                    f"{info['id']}-cancel", "ROLE_AGENT", reply, info["contextId"], info["id"]
                )
            return task
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:  # moved while we looked at it
            return self.task(path.stem.removeprefix("a2a-"), history_length, include_artifacts)
        header = HEADER_RE.match(raw)
        if not header:
            raise RpcError(-32603, "Internal error", "CORRUPT_TASK", {"file": path.name})
        task_id, context_id = header["task"], header["context"]
        start, end = raw.find(REQUEST_START), raw.find(REQUEST_END)
        request = ""
        if 0 <= start < end:
            request = raw[start + len(REQUEST_START) : end].strip("\n")
        answer = raw[end + len(REQUEST_END) :].strip() if end >= 0 else ""

        timestamp = header["created"]
        note, source = None, None
        if state == SUBMITTED:
            # A run that started after the message was written has listed it.
            run = self.active_run()
            created = datetime.fromisoformat(timestamp).replace(microsecond=0)
            if run and run["start"] >= created:
                state, note = WORKING, self.progress()
        else:
            moved = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            timestamp = iso(moved)
            if not answer:
                # Moved to done/ with nothing written: the run's final text is the answer.
                run = self.run_at(moved)
                if run and run["end"] is None:
                    state, note = WORKING, self.progress()
                elif (answer := self.run_result(run) or ""):
                    source = "run-transcript"
                else:
                    state = FAILED
                    note = "The agent closed the message without writing an answer."
        task = {"id": task_id, "contextId": context_id, "status": {"state": state, "timestamp": timestamp}}
        if note:
            task["status"]["message"] = text_message(f"{task_id}-status", "ROLE_AGENT", note, context_id, task_id)
        if state == COMPLETED and include_artifacts:
            artifact = {"artifactId": f"{task_id}-answer", "name": "answer",
                        "parts": [{"text": answer, "mediaType": "text/plain"}]}
            if source:
                artifact["metadata"] = {"answerSource": source}
            task["artifacts"] = [artifact]
        if history_length:
            history = [text_message(f"{task_id}-request", "ROLE_USER", request, context_id, task_id)]
            if state == COMPLETED:
                history.append(text_message(f"{task_id}-answer", "ROLE_AGENT", answer, context_id, task_id))
            task["history"] = history[-history_length:]
        return task

    def list(self, context_id=None, state=None, history_length=0, include_artifacts=False):
        found = []
        for folder, expected in (
            (self.config.inbox, SUBMITTED),
            (self.config.done, COMPLETED),
            (self.config.canceled, CANCELED),
        ):
            pattern = "*.json" if expected == CANCELED else "a2a-*.md"
            for path in folder.glob(pattern):
                if path.name.endswith("-cancel.md"):
                    continue
                if expected != CANCELED and (self.config.canceled / f"{path.stem.removeprefix('a2a-')}.json").exists():
                    continue  # canceled: listed once, from its marker
                try:
                    task = self._build(path, expected, history_length, include_artifacts)
                except (RpcError, json.JSONDecodeError, KeyError, OSError):
                    continue
                if context_id and task["contextId"] != context_id:
                    continue
                if state and task["status"]["state"] != state:
                    continue
                found.append(task)
        found.sort(key=lambda t: t["status"]["timestamp"], reverse=True)
        return found

    def cancel(self, task_id):
        """Asks the agent to stop: the request is a file of its own in the inbox.

        Removing the task's file would not do, the agent may already be working
        on it. The task is CANCELED for the client at once; the agent's reply to
        the cancel file, once written, is shown as the status message.
        """
        with self.lock:
            path, state = self.locate(task_id)
            if path is None:
                raise task_not_found(task_id)
            task = self._build(path, state, 0, False)
            if task["status"]["state"] not in (SUBMITTED, WORKING):
                raise RpcError(
                    -32002, "Task cannot be canceled", "TASK_NOT_CANCELABLE",
                    {"taskId": task_id, "state": task["status"]["state"]},
                )
            lines = [f"<!-- a2a cancel task={task_id} -->"]
            if self.config.preamble:
                lines.append(self.config.preamble)
            lines += [
                "",
                REQUEST_START,
                f"The A2A client canceled task {task_id} (file {self.filename(task_id)}, in inbox/ or "
                "already in inbox/done/). Stop working on it now, undo what you can, and do not "
                "answer it. Reply here in one line: what you stopped or undid.",
                REQUEST_END,
                "",
            ]
            temporary = self.config.inbox / f".{self.filename(task_id, cancel=True)}.tmp"
            temporary.write_text("\n".join(lines), encoding="utf-8")
            os.replace(temporary, self.config.inbox / self.filename(task_id, cancel=True))
            record = {"id": task_id, "contextId": task["contextId"], "canceled": iso(now())}
            marker = self.config.canceled / f"{task_id}.json"
            marker.write_text(json.dumps(record), encoding="utf-8")
            return self._build(marker, CANCELED, 0, False)


class AuthError(Exception):
    def __init__(self, status, error, description):
        super().__init__(description)
        self.status = status
        self.error = error
        self.description = description

    def www_authenticate(self):
        if not self.error:
            return 'Bearer realm="a2a"'
        return f'Bearer realm="a2a", error="{self.error}", error_description="{self.description}"'


class OidcVerifier:
    """Checks the JWTs of an OpenID Connect provider (Authentik, Keycloak, ...)."""

    def __init__(self, config):
        self.config = config
        self.lock = threading.Lock()
        self.client = None

    def _jwks_client(self):
        import jwt

        with self.lock:
            if self.client is None:
                url = self.config.oidc_jwks_url
                if not url:
                    discovery = self.config.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
                    with urllib.request.urlopen(discovery, timeout=5) as response:
                        url = json.load(response)["jwks_uri"]
                self.client = jwt.PyJWKClient(url, cache_keys=True, lifespan=3600, timeout=5)
            return self.client

    def verify(self, token):
        """Returns the claims of a valid token, raises AuthError otherwise."""
        import jwt

        unreachable = AuthError(503, "temporarily_unavailable", "identity provider unreachable")
        try:
            key = self._jwks_client().get_signing_key_from_jwt(token).key
            audience = self.config.oidc_audience
            claims = jwt.decode(
                token, key,
                algorithms=self.config.oidc_algorithms,
                issuer=self.config.oidc_issuer,
                audience=audience or None,
                leeway=30,
                options={"require": ["exp", "iss", "sub"], "verify_aud": bool(audience)},
            )
        except (jwt.PyJWKClientConnectionError, OSError) as error:
            log.warning("identity provider unreachable: %s", error)
            raise unreachable
        except (jwt.PyJWTError, ValueError, KeyError) as error:
            raise AuthError(401, "invalid_token", type(error).__name__)
        needed = self.config.oidc_scope
        if needed:
            scope = claims.get("scope", claims.get("scp", ""))
            granted = set(scope.split() if isinstance(scope, str) else scope)
            if needed not in granted:
                raise AuthError(403, "insufficient_scope", f"scope {needed} is required")
        allowed = self.config.oidc_allowed_subjects
        if allowed:
            names = {str(claims[k]) for k in ("sub", "preferred_username", "azp", "client_id", "email") if claims.get(k)}
            if not names & set(allowed):
                raise AuthError(403, "access_denied", "this caller is not allowed")
        return claims


class Bridge:
    """The A2A methods, independent of HTTP."""

    def __init__(self, config):
        self.config = config
        self.store = Store(config)
        self.verifier = OidcVerifier(config) if config.oidc_issuer else None
        self.local = threading.local()

    def agent_card(self):
        card = {
            "name": self.config.name,
            "description": self.config.description,
            "supportedInterfaces": [
                {
                    "url": f"{self.config.public_url}/rpc",
                    "protocolBinding": "JSONRPC",
                    "protocolVersion": PROTOCOL_VERSION,
                }
            ],
            "version": VERSION,
            "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": [
                {
                    "id": "ask-agent",
                    "name": "Ask the agent",
                    "description": "Leaves a message in the agent's inbox. The agent answers in its next run.",
                    "tags": ["agent", "inbox"],
                    "examples": ["Summarize what you did today."],
                    "inputModes": ["text/plain"],
                    "outputModes": ["text/plain"],
                }
            ],
        }
        schemes, requirements = {}, []
        if self.verifier:
            url = self.config.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
            schemes["oidc"] = {"openIdConnectSecurityScheme": {"openIdConnectUrl": url}}
            scopes = [self.config.oidc_scope] if self.config.oidc_scope else []
            requirements.append({"schemes": {"oidc": {"list": scopes}}})
        if self.config.token:
            schemes["bearer"] = {"httpAuthSecurityScheme": {"scheme": "Bearer"}}
            requirements.append({"schemes": {"bearer": {"list": []}}})
        if schemes:
            card["securitySchemes"] = schemes
            card["securityRequirements"] = requirements  # alternatives: one is enough
        return card

    @property
    def auth_required(self):
        return bool(self.config.token or self.verifier)

    def authenticate(self, header):
        """Returns the caller's name, or None when anonymous access is allowed.

        Raises AuthError. A token is accepted if it is the static token or a
        valid JWT of the configured identity provider.
        """
        if not self.auth_required:
            return None
        scheme, _, value = (header or "").partition(" ")
        value = value.strip()
        if scheme.lower() != "bearer" or not value:
            raise AuthError(401, None, "bearer token required")
        if self.config.token and hmac.compare_digest(value, self.config.token):
            return "static-token"
        if self.verifier and value.count(".") == 2:
            claims = self.verifier.verify(value)
            name = str(claims.get("azp") or claims.get("client_id") or claims["sub"])
            return re.sub(r"[^\w.@:-]", "", name)[:100] or "unknown"
        raise AuthError(401, "invalid_token", "unknown token")

    def check_version(self, version):
        if version and version.split(".")[0] != PROTOCOL_VERSION.split(".")[0]:
            raise RpcError(
                -32009, f"Protocol version {version} is not supported",
                "VERSION_NOT_SUPPORTED", {"supported": PROTOCOL_VERSION},
            )

    def handle(self, request, version=None, caller=None):
        """Handles one JSON-RPC request object, returns the response object."""
        self.local.caller = caller
        request_id = request.get("id") if isinstance(request, dict) else None
        try:
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
                raise invalid_request()
            self.check_version(version)
            params = request.get("params") or {}
            if not isinstance(params, dict):
                raise invalid_params("params must be an object")
            handler = {
                "SendMessage": self.send_message,
                "GetTask": self.get_task,
                "ListTasks": self.list_tasks,
                "CancelTask": self.cancel_task,
            }.get(request["method"])
            if handler is None:
                raise self.unsupported(request["method"])
            return {"jsonrpc": "2.0", "id": request_id, "result": handler(params)}
        except RpcError as error:
            return {"jsonrpc": "2.0", "id": request_id, "error": error.to_json()}
        except Exception:  # never leak internals to the client
            log.exception("internal error")
            return {"jsonrpc": "2.0", "id": request_id, "error": RpcError(-32603, "Internal error").to_json()}

    @staticmethod
    def unsupported(method):
        if method in ("SendStreamingMessage", "SubscribeToTask"):
            return RpcError(-32004, "Streaming is not supported", "UNSUPPORTED_OPERATION", {"method": method})
        if "PushNotificationConfig" in method:
            return RpcError(-32003, "Push notifications are not supported", "PUSH_NOTIFICATION_NOT_SUPPORTED")
        if method == "GetExtendedAgentCard":
            return RpcError(-32007, "No extended agent card is configured", "EXTENDED_AGENT_CARD_NOT_CONFIGURED")
        return RpcError(-32601, "Method not found")

    def send_message(self, params):
        message = params.get("message")
        if not isinstance(message, dict):
            raise invalid_params("message is required")
        if message.get("role") != "ROLE_USER":
            raise invalid_params("message.role must be ROLE_USER")
        parts = message.get("parts")
        if not isinstance(parts, list) or not parts:
            raise invalid_params("message.parts needs at least one part")
        texts = []
        for part in parts:
            if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                raise RpcError(-32005, "Only text parts are supported", "CONTENT_TYPE_NOT_SUPPORTED")
            texts.append(part["text"])
        text = "\n\n".join(texts).strip()
        if not text:
            raise invalid_params("message has no text")
        if message.get("taskId"):
            task_id = message["taskId"]
            if not self.store.locate(str(task_id))[0]:
                raise task_not_found(task_id)
            raise RpcError(
                -32004, "Tasks of this agent do not accept further messages: start a new task in the same contextId",
                "UNSUPPORTED_OPERATION", {"taskId": task_id},
            )
        context_id = message.get("contextId") or None
        if context_id is not None and not (isinstance(context_id, str) and CONTEXT_RE.match(context_id)):
            raise invalid_params("contextId must be 1-128 characters of A-Z a-z 0-9 . _ : -")

        configuration = params.get("configuration") or {}
        history_length = configuration.get("historyLength") or 0
        task_id = self.store.create(text, context_id, getattr(self.local, "caller", None))
        log.info("task %s submitted", task_id)

        if not configuration.get("returnImmediately"):
            deadline = time.monotonic() + self.config.block_timeout
            while time.monotonic() < deadline and not self.store.is_finished(task_id):
                time.sleep(self.config.poll_interval)
        return {"task": self.store.task(task_id, history_length)}

    def get_task(self, params):
        task_id = params.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise invalid_params("id is required")
        history_length = params.get("historyLength") or 0
        return self.store.task(task_id, history_length)

    def list_tasks(self, params):
        page_size = params.get("pageSize") or 50
        if not isinstance(page_size, int) or not 1 <= page_size <= 100:
            raise invalid_params("pageSize must be between 1 and 100")
        offset = 0
        if params.get("pageToken"):
            try:
                offset = int(params["pageToken"])
            except (TypeError, ValueError):
                raise invalid_params("pageToken is not valid")
        state = params.get("status") or None
        if state in ("TASK_STATE_UNSPECIFIED",):
            state = None
        tasks = self.store.list(
            params.get("contextId") or None,
            state,
            params.get("historyLength") or 0,
            bool(params.get("includeArtifacts")),
        )
        after = params.get("statusTimestampAfter")
        if after:
            tasks = [t for t in tasks if t["status"]["timestamp"] > after]
        page = tasks[offset : offset + page_size]
        more = offset + page_size < len(tasks)
        return {
            "tasks": page,
            "nextPageToken": str(offset + page_size) if more else "",
            "pageSize": page_size,
            "totalSize": len(tasks),
        }

    def cancel_task(self, params):
        task_id = params.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise invalid_params("id is required")
        return self.store.cancel(task_id)


def make_handler(bridge):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "a2a-bridge"

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def send_json(self, status, payload, headers=None):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
                self.send_json(200, bridge.agent_card(), {"Cache-Control": "public, max-age=300"})
            elif path == "/healthz":
                self.send_json(200, {"status": "ok"})
            else:
                self.send_json(404, {"error": "not found"})

        def do_POST(self):
            if self.path.split("?")[0] not in ("/", "/rpc"):
                return self.send_json(404, {"error": "not found"})
            try:
                caller = bridge.authenticate(self.headers.get("Authorization"))
            except AuthError as error:
                return self.send_json(
                    error.status, {"error": error.error or "unauthorized"},
                    {"WWW-Authenticate": error.www_authenticate()},
                )
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0 or length > bridge.config.max_body:
                self.close_connection = True
                return self.send_json(413, {"error": "payload too large"})
            body = self.rfile.read(length)
            try:
                request = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                response = {"jsonrpc": "2.0", "id": None, "error": parse_error().to_json()}
            else:
                version = self.headers.get("A2A-Version")
                if isinstance(request, list):
                    response = {"jsonrpc": "2.0", "id": None, "error": invalid_request("Batches are not supported").to_json()}
                else:
                    response = bridge.handle(request, version, caller)
            self.send_json(200, response, {"A2A-Version": PROTOCOL_VERSION})

    return Handler


def serve(config=None):
    config = config or Config()
    bridge = Bridge(config)
    server = ThreadingHTTPServer(("0.0.0.0", config.port), make_handler(bridge))
    server.daemon_threads = True
    log.info("A2A bridge %s on :%d, inbox %s, auth %s", VERSION, config.port, config.inbox,
             ", ".join(filter(None, [f"oidc {config.oidc_issuer}" if config.oidc_issuer else "",
                                     "static token" if config.token else ""])) or "NONE")
    server.serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    serve()
