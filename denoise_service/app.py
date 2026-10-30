"""The HTTP API: classify one spectrum, classify a batch, report health.

Design notes
- Every response is built with `orjson.dumps` and `Response(media_type="application/json")`.
- Request bodies are read by hand so the size limit is enforced on the bytes actually received
  (a missing or understated Content-Length cannot bypass it) and so validation errors can be
  reported as a short `{"detail": ...}` that never echoes posted values.
- Spectrum contents are never logged: one line per request with method, path, status, ion count
  and milliseconds.
- The rate limiter and the formula cache are process-wide, so run exactly one worker.
"""
import asyncio
import contextlib
import logging
import math
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple, Union

import orjson
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, StrictBool, StrictInt, StrictFloat, StrictStr
from pydantic import ValidationError

import denoise
from denoise_service import classify as classify_mod
from denoise_service.classify import Options
from denoise_service.config import Config
from denoise_service.limits import TokenBucket
from denoise_service.parse import SpectrumParseError, parse_spectrum

__all__ = ["create_app", "EXPECTED_FINGERPRINT"]

EXPECTED_FINGERPRINT = "5aae4ec26694da4d"
log = logging.getLogger("denoise_service.access")

_Number = Union[StrictInt, StrictFloat]


class SpectrumRequest(BaseModel):
    """Field types for one classify body; limits that depend on the config are checked after."""
    mode: StrictStr
    peaks: Optional[List[Any]] = None
    spectrum: Optional[StrictStr] = None
    halogens: StrictBool = False
    deuterium: StrictInt = 0
    carbon13: StrictInt = 0
    nitrogen15: StrictInt = 0
    precursor_mz: Optional[_Number] = None
    id: Optional[Union[StrictStr, StrictInt]] = None


_FIELDS = {"mode", "peaks", "spectrum", "halogens", "deuterium", "carbon13", "nitrogen15",
           "precursor_mz", "id"}


class ApiError(Exception):
    def __init__(self, status: int, detail: str, headers: Optional[Dict[str, str]] = None):
        super().__init__(detail)
        self.status, self.detail, self.headers = status, detail, headers or {}


def _json(status: int, payload: Any, headers: Optional[Dict[str, str]] = None) -> Response:
    return Response(content=orjson.dumps(payload), status_code=status,
                    media_type="application/json", headers=headers)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _model_error(exc: ValidationError) -> str:
    # Field name and reason only; never the offending input.
    first = exc.errors()[0]
    loc = ".".join(str(p) for p in first.get("loc", ()) if p != "__root__") or "body"
    return "%s: %s" % (loc, first.get("msg", "invalid"))


def _validate(raw: Any, cfg: Config, where: str = "") -> Tuple[List[Tuple[float, float]], Options, Optional[Any]]:
    """Return (peaks, options, id) for one classify body, or raise ApiError(422)."""
    def bad(msg: str) -> ApiError:
        return ApiError(422, where + msg)

    if not isinstance(raw, dict):
        raise bad("body must be a JSON object")
    unknown = sorted(k for k in raw if k not in _FIELDS)
    if unknown:
        key = unknown[0]
        raise bad("unknown field: %s" % (key if len(key) <= 32 else key[:32] + "..."))
    try:
        req = SpectrumRequest(**raw)
    except ValidationError as exc:
        raise bad(_model_error(exc))
    if req.mode not in ("pos", "neg"):
        raise bad('mode: must be "pos" or "neg"')
    if (req.peaks is None) == (req.spectrum is None):
        raise bad('give exactly one of "peaks" or "spectrum"')
    for name in ("deuterium", "carbon13", "nitrogen15"):
        v = getattr(req, name)
        if v < 0 or v > cfg.max_label:
            raise bad("%s: must be between 0 and %d" % (name, cfg.max_label))
    lookups = (req.deuterium + 1) * (req.carbon13 + 1) * (req.nitrogen15 + 1)
    if lookups > cfg.max_label_lookups:
        raise bad("label counts too large: (d+1)*(c+1)*(n+1) must be at most %d"
                  % cfg.max_label_lookups)
    if req.precursor_mz is not None and not (math.isfinite(req.precursor_mz)
                                             and req.precursor_mz > 0):
        raise bad("precursor_mz: must be a finite number greater than zero")
    if req.peaks is not None:
        if len(req.peaks) > cfg.max_peaks:
            raise bad("too many peaks: at most %d allowed" % cfg.max_peaks)
        peaks = []  # type: List[Tuple[float, float]]
        for i, p in enumerate(req.peaks):
            if not (isinstance(p, (list, tuple)) and len(p) == 2
                    and _is_number(p[0]) and _is_number(p[1])):
                raise bad("peaks[%d]: must be a pair [mz, intensity] of numbers" % i)
            mz, inten = float(p[0]), float(p[1])
            if not (math.isfinite(mz) and math.isfinite(inten)):
                raise bad("peaks[%d]: values must be finite" % i)
            if mz <= 0:
                raise bad("peaks[%d]: m/z must be greater than zero" % i)
            if inten < 0:
                raise bad("peaks[%d]: intensity must not be negative" % i)
            peaks.append((mz, inten))
        if not peaks:
            raise bad("no peaks found")
    else:
        try:
            peaks = parse_spectrum(req.spectrum or "")
        except SpectrumParseError as exc:
            # Line and reason only: the exception's own message quotes the posted token.
            raise bad("line %d: %s" % (exc.line, exc.reason) if exc.line else exc.reason)
        if len(peaks) > cfg.max_peaks:
            raise bad("too many peaks: at most %d allowed" % cfg.max_peaks)
    options = Options(mode=req.mode, halogens=req.halogens, deuterium=req.deuterium,
                      carbon13=req.carbon13, nitrogen15=req.nitrogen15,
                      precursor_mz=None if req.precursor_mz is None else float(req.precursor_mz))
    return peaks, options, req.id


def _work(peaks: List[Tuple[float, float]], options: Options) -> int:
    """Lookups one spectrum costs: peaks x label combinations."""
    return len(peaks) * (options.deuterium + 1) * (options.carbon13 + 1) * (options.nitrogen15 + 1)


def _check_budget(total: int, cfg: Config) -> None:
    if total > cfg.max_request_lookups:
        raise ApiError(422, "request too large: work budget is %d (peaks x label combinations "
                       "summed over spectra)" % cfg.max_request_lookups)


def _run(peaks: List[Tuple[float, float]], options: Options) -> Dict[str, Any]:
    result = classify_mod.classify(peaks, options)
    return {
        "fingerprint": classify_mod.fingerprint(),
        "mode": options.mode,
        "ions": [{"mz": mz, "intensity": float(inten), "possible": ok}
                 for (mz, inten), ok in zip(peaks, result.possible)],
        "summary": result.summary,
    }


def _clear_cache() -> None:
    denoise.clear_cache()


def _check_tables() -> None:
    for name, status in (("table", denoise.table_status()),
                         ("halogen table", denoise.halogen_table_status())):
        if not str(status).startswith("loaded"):
            raise RuntimeError("%s not usable: %s" % (name, status))
    got = classify_mod.fingerprint()
    if got != EXPECTED_FINGERPRINT:
        raise RuntimeError("chemistry fingerprint %s != expected %s" % (got, EXPECTED_FINGERPRINT))


def create_app(config: Config) -> FastAPI:
    limiter = TokenBucket(config.rate_per_minute, config.rate_burst)

    # One dedicated worker thread owns all classification and the denoise cache.
    state = {"executor": None, "pending": 0}  # type: Dict[str, Any]

    async def _clear_loop() -> None:
        loop = asyncio.get_event_loop()
        while True:
            await asyncio.sleep(config.cache_clear_seconds)
            await loop.run_in_executor(state["executor"], _clear_cache)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        _check_tables()
        state["executor"] = ThreadPoolExecutor(max_workers=1, thread_name_prefix="denoise-worker")
        state["pending"] = 0
        task = asyncio.ensure_future(_clear_loop())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            state["executor"].shutdown(wait=True)

    app = FastAPI(title="denoise service", lifespan=lifespan,
                  docs_url="/docs", redoc_url=None, openapi_url="/openapi.json")
    app.add_middleware(CORSMiddleware, allow_origins=config.cors_origins,
                       allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["*"],
                       allow_credentials=False)

    def client_key(request: Request) -> str:
        if config.trusted_proxy:
            fwd = request.headers.get("x-forwarded-for", "")
            first = fwd.split(",")[0].strip()
            if first:
                return first
        return request.client.host if request.client else "unknown"

    async def read_body(request: Request) -> bytes:
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > config.max_body_bytes:
            raise ApiError(413, "request body too large (limit %d bytes)" % config.max_body_bytes)
        chunks = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > config.max_body_bytes:
                raise ApiError(413, "request body too large (limit %d bytes)"
                               % config.max_body_bytes)
            chunks.append(chunk)
        return b"".join(chunks)

    async def handle(request: Request, work) -> Response:
        start = time.monotonic()
        ions = None  # type: Optional[int]
        try:
            allowed, wait = limiter.allow(client_key(request), time.monotonic())
            if not allowed:
                raise ApiError(429, "rate limit exceeded; retry later",
                               {"Retry-After": str(max(1, int(math.ceil(wait))))})
            body = await read_body(request)
            try:
                raw = orjson.loads(body)
            except orjson.JSONDecodeError:
                raise ApiError(422, "body is not valid JSON")
            compute, ions = work(raw)
            if state["pending"] >= config.max_pending:
                raise ApiError(503, "server busy; retry later", {"Retry-After": "1"})
            state["pending"] += 1
            try:
                payload = await asyncio.get_event_loop().run_in_executor(
                    state["executor"], compute)
            finally:
                state["pending"] -= 1
            response = _json(200, payload)
        except ApiError as err:
            response = _json(err.status, {"detail": err.detail}, err.headers)
        log.info("%s %s %d ions=%s %.1fms", request.method, request.url.path,
                 response.status_code, "-" if ions is None else ions,
                 (time.monotonic() - start) * 1000.0)
        return response

    def classify_work(raw: Any):
        peaks, options, _ = _validate(raw, config)
        _check_budget(_work(peaks, options), config)
        return (lambda: _run(peaks, options)), len(peaks)

    def batch_work(raw: Any):
        if not isinstance(raw, dict) or set(raw) - {"spectra"}:
            raise ApiError(422, 'body must be an object with only "spectra"')
        spectra = raw.get("spectra")
        if not isinstance(spectra, list) or not spectra:
            raise ApiError(422, "spectra: must be a non-empty list")
        if len(spectra) > config.max_batch:
            raise ApiError(422, "too many spectra: at most %d allowed" % config.max_batch)
        parsed = [_validate(s, config, "spectra[%d]: " % i) for i, s in enumerate(spectra)]
        _check_budget(sum(_work(p, o) for p, o, _ in parsed), config)

        def compute() -> Dict[str, Any]:
            results = []
            for peaks, options, ident in parsed:
                out = _run(peaks, options)
                out["id"] = ident
                results.append(out)
            return {"results": results}
        return compute, sum(len(p[0]) for p in parsed)

    @app.post("/v1/classify")
    async def classify_endpoint(request: Request) -> Response:
        return await handle(request, classify_work)

    @app.post("/v1/classify/batch")
    async def batch_endpoint(request: Request) -> Response:
        return await handle(request, batch_work)

    @app.get("/healthz")
    async def healthz() -> Response:
        return _json(200, {
            "status": "ok",
            "fingerprint": classify_mod.fingerprint(),
            "tables": {"chnops": denoise.table_status(),
                       "halogen": denoise.halogen_table_status()},
            "cache_size": denoise.cache_size(),
        })

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> Response:
        # FastAPI's default body echoes the input; report field and reason only.
        errs = exc.errors()
        first = errs[0] if errs else {}
        loc = ".".join(str(p) for p in first.get("loc", ()))
        return _json(422, {"detail": "%s: %s" % (loc or "request", first.get("msg", "invalid"))})

    # Registered last so the page and its assets can never shadow an API route.
    static_dir = Path(__file__).resolve().parent / "static"

    @app.get("/", include_in_schema=False)
    async def index() -> Response:
        return FileResponse(static_dir / "index.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app
