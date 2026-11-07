"""Minimal thread-safe WebDriver BiDi client for Firefox."""

from __future__ import annotations

import itertools
import json
import math
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

import websocket  # websocket-client


class BiDiError(Exception):
    """An error response returned by the browser."""

    def __init__(self, error: str, message: str = ""):
        super().__init__(f"{error}: {message}" if message else error)
        self.error = error
        self.message = message


class ScriptError(Exception):
    """A JavaScript exception thrown by a script we ran in the page."""


class ConnectionLost(Exception):
    """The WebSocket connection to Firefox closed."""


@dataclass(frozen=True)
class Channel:
    """A script.ChannelValue argument: lets page scripts send us messages."""

    name: str


@dataclass(frozen=True)
class WindowRef:
    """A serialized WindowProxy, i.e. a reference to a browsing context."""

    context: str


def to_local(value: Any) -> dict:
    """Convert a Python value into a BiDi script.LocalValue."""
    if isinstance(value, Channel):
        return {"type": "channel", "value": {"channel": value.name, "ownership": "none"}}
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean", "value": value}
    if isinstance(value, (int, float)):
        if isinstance(value, float):
            if math.isnan(value):
                return {"type": "number", "value": "NaN"}
            if math.isinf(value):
                return {"type": "number", "value": "Infinity" if value > 0 else "-Infinity"}
        return {"type": "number", "value": value}
    if isinstance(value, str):
        return {"type": "string", "value": value}
    if isinstance(value, (list, tuple)):
        return {"type": "array", "value": [to_local(v) for v in value]}
    if isinstance(value, dict):
        return {"type": "object", "value": [[str(k), to_local(v)] for k, v in value.items()]}
    raise TypeError(f"Cannot send {type(value).__name__} to the page")


_SPECIAL_NUMBERS = {"NaN": math.nan, "-0": -0.0, "Infinity": math.inf, "-Infinity": -math.inf}


def from_remote(value: Any) -> Any:
    """Convert a BiDi script.RemoteValue into a plain Python value."""
    if not isinstance(value, dict):
        return value
    kind = value.get("type")
    if kind in ("undefined", "null"):
        return None
    if kind in ("string", "boolean", "bigint"):
        return value.get("value")
    if kind == "number":
        v = value.get("value")
        return _SPECIAL_NUMBERS.get(v, v) if isinstance(v, str) else v
    if kind in ("array", "set"):
        return [from_remote(v) for v in value.get("value") or []]
    if kind in ("object", "map"):
        out = {}
        for key, v in value.get("value") or []:
            out[key if isinstance(key, str) else str(from_remote(key))] = from_remote(v)
        return out
    if kind == "window":
        return WindowRef(value["value"]["context"])
    return value  # nodes, functions, ... are returned as-is


class BiDiClient:
    """Blocking command API on top of a background WebSocket reader.

    * ``send()`` may be called from any thread. Events are delivered on a
      dedicated thread, so event handlers may issue commands without
      deadlocking the WebSocket reader.
    * ``on_event(method, params)`` is invoked for every BiDi event.
    * ``on_close(reason)`` is invoked once if the connection drops.
    """

    def __init__(self,
                 on_event: Optional[Callable[[str, dict], None]] = None,
                 on_close: Optional[Callable[[str], None]] = None):
        self.on_event = on_event
        self.on_close = on_close
        self._ws: Optional[websocket.WebSocket] = None
        self._ids = itertools.count(1)
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._events: queue.Queue = queue.Queue()
        self._closed = threading.Event()
        self._closing = False
        self.session_id: Optional[str] = None
        self.capabilities: dict = {}

    # -- connection -------------------------------------------------------

    def connect(self, port: int, host: str = "127.0.0.1", timeout: float = 5.0) -> dict:
        url = f"ws://{host}:{port}/session"
        # Firefox rejects WebSocket upgrades that carry an unexpected Origin.
        self._ws = websocket.create_connection(url, timeout=timeout, suppress_origin=True)
        self._ws.settimeout(None)
        threading.Thread(target=self._reader, name="bidi-reader", daemon=True).start()
        threading.Thread(target=self._dispatcher, name="bidi-events", daemon=True).start()
        result = self.send("session.new", {
            "capabilities": {"alwaysMatch": {
                # Never auto-dismiss alert()/confirm()/beforeunload/file dialogs
                # in the user's pages: leave them for the user to answer.
                "unhandledPromptBehavior": {"default": "ignore"},
            }}
        }, timeout=30)
        self.session_id = result.get("sessionId")
        self.capabilities = result.get("capabilities", {})
        return self.capabilities

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._closed.is_set()

    def close(self) -> None:
        """End the session (so Firefox accepts a new one later) and disconnect."""
        if self._ws is None:
            return
        self._closing = True
        if self.session_id and not self._closed.is_set():
            try:
                self.send("session.end", {}, timeout=3)
            except Exception:
                pass
        try:
            self._ws.close()
        except Exception:
            pass
        self._closed.wait(2)

    # -- commands ---------------------------------------------------------

    def _post(self, method: str, params: dict) -> tuple[int, queue.Queue]:
        if self._ws is None or self._closed.is_set():
            raise ConnectionLost("Not connected to Firefox")
        cmd_id = next(self._ids)
        q: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[cmd_id] = q
        try:
            self._ws.send(json.dumps({"id": cmd_id, "method": method, "params": params}))
        except Exception as exc:
            with self._lock:
                self._pending.pop(cmd_id, None)
            raise ConnectionLost(str(exc)) from exc
        return cmd_id, q

    def _wait(self, cmd_id: int, q: queue.Queue, method: str, timeout: float) -> dict:
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._pending.pop(cmd_id, None)
            raise TimeoutError(f"Firefox did not answer {method} within {timeout:g}s") from None
        if msg is None:
            raise ConnectionLost("Connection to Firefox closed")
        if msg.get("type") == "error":
            raise BiDiError(msg.get("error", "unknown error"), msg.get("message", ""))
        return msg.get("result", {})

    def send(self, method: str, params: Optional[dict] = None, timeout: float = 30.0) -> dict:
        cmd_id, q = self._post(method, params or {})
        return self._wait(cmd_id, q, method, timeout)

    def send_nowait(self, method: str, params: dict) -> None:
        """Fire-and-forget command (its reply is discarded)."""
        try:
            self._post(method, params)
        except ConnectionLost:
            pass

    def send_many(self, commands: Iterable[tuple[str, dict]], timeout: float = 10.0) -> list:
        """Send several commands at once; return a result or exception for each."""
        posted = []
        for method, params in commands:
            try:
                posted.append((method, *self._post(method, params)))
            except Exception as exc:  # noqa: BLE001 - reported per command
                posted.append((method, exc, None))
        results = []
        for method, cmd_id, q in posted:
            if q is None:
                results.append(cmd_id)  # the exception
                continue
            try:
                results.append(self._wait(cmd_id, q, method, timeout))
            except Exception as exc:  # noqa: BLE001
                results.append(exc)
        return results

    # -- script helpers ---------------------------------------------------

    def call(self, context: str, function: str, *args: Any, sandbox: Optional[str] = "tab-autoclicker",
             timeout: float = 10.0) -> Any:
        """Run a JS function in a browsing context and return its result."""
        return self.script_result(self.send("script.callFunction",
                                             self.call_params(context, function, *args, sandbox=sandbox),
                                             timeout=timeout))

    @staticmethod
    def call_params(context: str, function: str, *args: Any, sandbox: Optional[str] = "tab-autoclicker") -> dict:
        target = {"context": context}
        if sandbox:
            target["sandbox"] = sandbox
        return {
            "functionDeclaration": function,
            "target": target,
            "arguments": [to_local(a) for a in args],
            "awaitPromise": False,
            "resultOwnership": "none",
        }

    @staticmethod
    def script_result(result: dict) -> Any:
        if result.get("type") == "exception":
            details = result.get("exceptionDetails", {})
            raise ScriptError(details.get("text") or "script error")
        return from_remote(result.get("result"))

    # -- background threads -------------------------------------------------

    def _reader(self) -> None:
        reason = "closed"
        try:
            while True:
                raw = self._ws.recv()
                if not raw:
                    break
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                cmd_id = msg.get("id")
                if cmd_id is not None:
                    with self._lock:
                        q = self._pending.pop(cmd_id, None)
                    if q is not None:
                        q.put(msg)
                elif msg.get("type") == "event":
                    self._events.put((msg.get("method"), msg.get("params", {})))
        except Exception as exc:  # noqa: BLE001
            reason = str(exc) or type(exc).__name__
        finally:
            self._closed.set()
            with self._lock:
                pending, self._pending = self._pending, {}
            for q in pending.values():
                q.put(None)
            self._events.put(None)
            if self.on_close and not self._closing:
                try:
                    self.on_close(reason)
                except Exception:
                    pass

    def _dispatcher(self) -> None:
        while True:
            item = self._events.get()
            if item is None:
                return
            if self.on_event:
                try:
                    self.on_event(*item)
                except Exception:  # noqa: BLE001 - never kill the event thread
                    import traceback
                    traceback.print_exc()
