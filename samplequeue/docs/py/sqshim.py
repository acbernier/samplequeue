"""A tiny stand-in for FastAPI so the real backend runs inside the browser.

The app's routes in `app/main.py` are written against FastAPI's decorators.
Pyodide (Python in WebAssembly) can run them unchanged if something answers
to the same names, so this module provides just the parts main.py uses:
route registration, path/query/body binding, HTTPException and a few
response classes. `handle()` takes what the page's fetch() sent and returns
what it should get back.
"""
import inspect
import json
import re
import sys
import types
import urllib.parse


class HTTPException(Exception):
    def __init__(self, status_code, detail=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class Request:
    def __init__(self, headers, path, query):
        self.headers = headers
        self.query_params = query
        self.url = types.SimpleNamespace(path=path)


class _Body:
    def __init__(self, default=...):
        self.default = default


def Body(default=..., **_):
    return _Body(default)


class Response:
    media_type = "application/json"

    def __init__(self, content=None, status_code=200, headers=None, media_type=None):
        self.content = content
        self.status_code = status_code
        self.headers = dict(headers or {})
        if media_type:
            self.media_type = media_type


class JSONResponse(Response):
    pass


class HTMLResponse(Response):
    media_type = "text/html"


class StreamingResponse(Response):
    def __init__(self, content, **kw):
        super().__init__("".join(content), **kw)


class StaticFiles:
    def __init__(self, *_, **__):
        pass


def _compile(template):
    def repl(m):
        name, _, kind = m.group(1).partition(":")
        return "(?P<%s>%s)" % (name, ".+" if kind == "path" else "[^/]+")
    return re.compile(re.sub(r"\{([^}]+)\}", repl, template))


def _cast(value, annotation):
    if annotation is int:
        return int(value)
    if annotation is float:
        return float(value)
    if annotation is bool:
        return str(value).lower() in ("1", "true", "yes")
    return value


class FastAPI:
    def __init__(self, **_):
        self.routes = []

    def _route(self, method, path):
        def deco(fn):
            self.routes.append((method, _compile(path), fn))
            return fn
        return deco

    def get(self, path, **_):
        return self._route("GET", path)

    def post(self, path, **_):
        return self._route("POST", path)

    def put(self, path, **_):
        return self._route("PUT", path)

    def delete(self, path, **_):
        return self._route("DELETE", path)

    def _noop(self, *_, **__):
        return lambda fn: fn

    on_event = middleware = exception_handler = _noop

    def mount(self, *_, **__):
        pass

    # ---------------------------------------------------------------- dispatch

    def _bind(self, fn, params, request, body):
        kwargs = {}
        for name, p in inspect.signature(fn).parameters.items():
            ann, default = p.annotation, p.default
            if name in params:
                kwargs[name] = _cast(urllib.parse.unquote(params[name]), ann)
            elif ann is Request:
                kwargs[name] = request
            elif isinstance(default, _Body) or ann is dict:
                fallback = default.default if isinstance(default, _Body) else ...
                if body is None:
                    if fallback is ...:
                        raise HTTPException(422, "request body is required")
                    kwargs[name] = fallback
                else:
                    kwargs[name] = body
            elif name in request.query_params:
                kwargs[name] = _cast(request.query_params[name], ann)
            elif default is inspect.Parameter.empty:
                raise HTTPException(422, "missing query parameter %r" % name)
        return kwargs

    def dispatch(self, method, url, body_text, headers):
        parsed = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
        request = Request(headers, parsed.path, query)
        try:
            body = json.loads(body_text) if body_text else None
            for m, rx, fn in self.routes:
                if m != method:
                    continue
                hit = rx.fullmatch(parsed.path)
                if hit:
                    result = fn(**self._bind(fn, hit.groupdict(), request, body))
                    break
            else:
                raise HTTPException(404, "Not Found")
        except HTTPException as exc:
            return exc.status_code, "application/json", {}, json.dumps({"detail": exc.detail})
        except Exception as exc:  # same shape the real app returns for a crash
            return 500, "application/json", {}, json.dumps({"detail": str(exc)})

        if isinstance(result, Response):
            content = result.content
            if not isinstance(content, str):
                content = json.dumps(content, default=str)
            return result.status_code, result.media_type, result.headers, content
        return 200, "application/json", {}, json.dumps(result, default=str)


def install():
    """Make `import fastapi` resolve to this module."""
    root = types.ModuleType("fastapi")
    root.FastAPI, root.Body, root.Request = FastAPI, Body, Request
    root.HTTPException = HTTPException
    responses = types.ModuleType("fastapi.responses")
    responses.HTMLResponse, responses.JSONResponse = HTMLResponse, JSONResponse
    responses.StreamingResponse = StreamingResponse
    static = types.ModuleType("fastapi.staticfiles")
    static.StaticFiles = StaticFiles
    root.responses, root.staticfiles = responses, static
    sys.modules.update({"fastapi": root, "fastapi.responses": responses,
                        "fastapi.staticfiles": static})


_app = None


def start():
    """Import the real backend and set up a fresh or restored database."""
    global _app
    install()
    from app import main
    _app = main.app
    return main


def handle(method, url, body_text, actor):
    status, media_type, headers, body = _app.dispatch(
        method, url, body_text, {"X-Actor": actor or ""})
    return json.dumps({"status": status, "type": media_type,
                       "headers": headers, "body": body})
