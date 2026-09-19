"""FastAPI transport kept deliberately thin around an application service."""

from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Annotated, Any, Protocol

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agentic_context_service.adapters.observability import (
    emit_retrieval_audit,
    metrics_payload,
    observe_freshness,
    observe_operation,
    span,
)
from agentic_context_service.adapters.opa import PolicyUnavailableError
from agentic_context_service.adapters.service import ContextStaleError, PolicyDeniedError
from agentic_context_service.api.models import (
    BatchRetrieveRequest,
    FeedbackRequest,
    MemoryCreateRequest,
    MemoryPatchRequest,
    MemorySearchRequest,
    RetrieveRequest,
)
from agentic_context_service.api.request_context import (
    BearerAuthenticator,
    CanonicalRequestContext,
    RequestContextError,
    RequestContextVerifier,
)


class ContextApplication(Protocol):
    """Narrow application boundary consumed by HTTP."""

    async def execute(
        self,
        operation: str,
        context: CanonicalRequestContext,
        payload: dict[str, Any],
    ) -> Any: ...

    async def ready(self) -> bool: ...


ContextDependency = Callable[[Request], Awaitable[CanonicalRequestContext]]
Dispatcher = Callable[[str, CanonicalRequestContext, dict[str, Any]], Awaitable[Any]]


def create_app(
    *,
    service: ContextApplication,
    signing_secret: bytes,
    authenticator: BearerAuthenticator,
) -> FastAPI:
    """Build the transport with explicit dependencies for tests and production."""
    app = FastAPI(title="Agentic Context Service", version="0.1.0")
    _register_error_handlers(app)
    verifier = RequestContextVerifier(signing_secret, authenticator=authenticator)
    trusted_context = _trusted_context(verifier)
    dispatch = _dispatcher(service)
    _register_retrieval_routes(app, dispatch, trusted_context)
    _register_memory_routes(app, dispatch, trusted_context)
    _register_evidence_routes(app, dispatch, trusted_context)
    _register_health_routes(app, service)
    _register_metrics_route(app)
    return app


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, _error: RequestValidationError) -> JSONResponse:
        return _error_response(request, status.HTTP_400_BAD_REQUEST)

    @app.exception_handler(HTTPException)
    async def public_http_error(request: Request, error: HTTPException) -> JSONResponse:
        return _error_response(request, error.status_code)

    @app.exception_handler(Exception)
    async def internal_error(request: Request, _error: Exception) -> JSONResponse:
        return _error_response(request, status.HTTP_500_INTERNAL_SERVER_ERROR)


def _error_response(request: Request, status_code: int) -> JSONResponse:
    code, message, retryable = _PUBLIC_ERRORS.get(
        status_code,
        ("INTERNAL_ERROR", "internal server error", True),
    )
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "request_id": request.headers.get("X-Request-ID", "unknown"),
            "retryable": retryable,
        },
    )


def _trusted_context(verifier: RequestContextVerifier) -> ContextDependency:
    async def dependency(request: Request) -> CanonicalRequestContext:
        try:
            return verifier.verify(request.headers)
        except RequestContextError as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(error),
            ) from error

    return dependency


def _dispatcher(service: ContextApplication) -> Dispatcher:
    async def dispatch(
        operation: str,
        context: CanonicalRequestContext,
        payload: dict[str, Any],
    ) -> Any:
        started = perf_counter()
        outcome = "success"
        try:
            with span(
                f"context.{operation}",
                {
                    "tenant.id": context.tenant_id,
                    "workflow.id": context.workflow_id,
                    "workflow.revision": context.workflow_revision,
                },
            ):
                result = await service.execute(operation, context, payload)
            elapsed = perf_counter() - started
            _record_success(operation, context, payload, result, elapsed)
            return result
        except Exception as error:
            outcome, translated = _translate_error(error)
            if translated is error:
                raise
            raise translated from error
        finally:
            observe_operation(operation, outcome, perf_counter() - started)

    return dispatch


def _record_success(
    operation: str,
    context: CanonicalRequestContext,
    payload: dict[str, Any],
    result: Any,
    elapsed: float,
) -> None:
    if operation == "context.retrieve" and isinstance(result, dict):
        emit_retrieval_audit(
            operation=operation,
            context=context,
            payload=payload,
            result=result,
            latency_ms=elapsed * 1_000,
        )
    if operation == "source.freshness" and isinstance(result, dict):
        observe_freshness(str(payload["source"]), result.get("lag_seconds"))


def _translate_error(error: Exception) -> tuple[str, Exception]:
    if isinstance(error, PolicyUnavailableError):
        return "dependency_error", HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail="authorization dependency unavailable",
        )
    if isinstance(error, PolicyDeniedError):
        return "denied", HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="forbidden",
        )
    if isinstance(error, ContextStaleError):
        return "stale", HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="CONTEXT_STALE",
        )
    if isinstance(error, KeyError):
        return "not_found", HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="not found",
        )
    return "error", error


def _register_metrics_route(app: FastAPI) -> None:
    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        payload, content_type = metrics_payload()
        return Response(content=payload, media_type=content_type)


def _register_health_routes(app: FastAPI, service: ContextApplication) -> None:
    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(response: Response) -> dict[str, str]:
        try:
            is_ready = await service.ready()
        except Exception:
            is_ready = False
        if not is_ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "not_ready"}
        return {"status": "ok"}


def _register_retrieval_routes(
    app: FastAPI,
    dispatch: Dispatcher,
    trusted_context: ContextDependency,
) -> None:
    @app.post("/v1/context:retrieve")
    async def retrieve(
        body: RetrieveRequest,
        context: Annotated[CanonicalRequestContext, Depends(trusted_context)],
    ) -> Any:
        return await dispatch("context.retrieve", context, body.model_dump())

    @app.post("/v1/context:batchRetrieve")
    async def batch_retrieve(
        body: BatchRetrieveRequest,
        context: Annotated[CanonicalRequestContext, Depends(trusted_context)],
    ) -> Any:
        return await dispatch("context.batchRetrieve", context, body.model_dump())


def _register_memory_routes(
    app: FastAPI,
    dispatch: Dispatcher,
    trusted_context: ContextDependency,
) -> None:
    @app.post("/v1/memories", status_code=status.HTTP_201_CREATED)
    async def create_memory(
        body: MemoryCreateRequest,
        context: Annotated[CanonicalRequestContext, Depends(trusted_context)],
    ) -> Any:
        return await dispatch("memory.create", context, body.model_dump())

    @app.post("/v1/memories:search")
    async def search_memories(
        body: MemorySearchRequest,
        context: Annotated[CanonicalRequestContext, Depends(trusted_context)],
    ) -> Any:
        return await dispatch("memory.search", context, body.model_dump())

    @app.patch("/v1/memories/{id}")
    async def patch_memory(
        id: str,
        body: MemoryPatchRequest,
        context: Annotated[CanonicalRequestContext, Depends(trusted_context)],
    ) -> Any:
        return await dispatch(
            "memory.patch",
            context,
            {"id": id, **body.model_dump(exclude_none=True)},
        )

    @app.delete("/v1/memories/{id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_memory(
        id: str,
        context: Annotated[CanonicalRequestContext, Depends(trusted_context)],
    ) -> Response:
        await dispatch("memory.delete", context, {"id": id})
        return Response(status_code=status.HTTP_204_NO_CONTENT)


def _register_evidence_routes(
    app: FastAPI,
    dispatch: Dispatcher,
    trusted_context: ContextDependency,
) -> None:
    @app.post("/v1/feedback", status_code=status.HTTP_202_ACCEPTED)
    async def feedback(
        body: FeedbackRequest,
        context: Annotated[CanonicalRequestContext, Depends(trusted_context)],
    ) -> Any:
        return await dispatch("feedback.create", context, body.model_dump())

    @app.get("/v1/sources/{source}/freshness")
    async def source_freshness(
        source: str,
        context: Annotated[CanonicalRequestContext, Depends(trusted_context)],
    ) -> Any:
        return await dispatch("source.freshness", context, {"source": source})


_PUBLIC_ERRORS: dict[int, tuple[str, str, bool]] = {
    status.HTTP_400_BAD_REQUEST: ("INVALID_REQUEST", "invalid request", False),
    status.HTTP_401_UNAUTHORIZED: ("UNAUTHENTICATED", "authentication required", False),
    status.HTTP_403_FORBIDDEN: ("POLICY_DENIED", "forbidden", False),
    status.HTTP_404_NOT_FOUND: ("NOT_FOUND", "not found", False),
    status.HTTP_409_CONFLICT: ("CONTEXT_STALE", "context is stale", False),
    status.HTTP_424_FAILED_DEPENDENCY: (
        "AUTHORIZATION_DEPENDENCY_UNAVAILABLE",
        "authorization dependency unavailable",
        True,
    ),
    status.HTTP_503_SERVICE_UNAVAILABLE: ("DEPENDENCY_UNAVAILABLE", "dependency unavailable", True),
}
