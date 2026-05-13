"""HTTP method router factory.

Centralises the ``REGISTRY_HTTP_METHODS_MODE`` and
``REGISTRY_HTTP_METHOD_ALIAS_SEPARATOR`` logic so every mutation route in
every phase is registered consistently.

Usage
-----
::

    from fastapi import APIRouter
    from registry.api.middleware.http_methods import HttpMethodRouter, get_mode_settings

    base = APIRouter(prefix="/v1/capabilities", tags=["capabilities"])
    mode, sep = get_mode_settings()
    mr = HttpMethodRouter(base, mode=mode, separator=sep)

    mr.add_mutation_route(
        path="/{entity_id}",
        action="update",
        handler=patch_capability,   # defined once, registered under both surfaces
        verb="PATCH",
        response_model=CapabilityResponse,
    )

    # Include `base` in the FastAPI app as normal.

Mode semantics
--------------
* ``rest``      — verb-conventional routes only (PATCH /…, DELETE /…, …)
* ``post_only`` — POST-tunneled aliases only (POST /…:update, POST /…:delete, …)
* ``both``      — both surfaces (default)

Separator semantics
-------------------
* ``colon`` — ``POST /v1/capabilities/{id}:delete``   (default, RFC 3986 unreserved)
* ``slash``  — ``POST /v1/capabilities/{id}/delete``

DELETE idempotency (RFC 9110 §9.3.5)
-------------------------------------
The service layer is responsible for the idempotency contract:

* First DELETE on a live row → 204 No Content (set ``t_invalidated_at``)
* Repeat DELETE on already-invalidated row → 204 No Content (no-op; audit row with action='delete_noop_idempotent')
* DELETE on hard-purged / never-existing ID → 404 Not Found

Handlers that use :func:`soft_delete_idempotent` receive a pre-built helper
that reads the ``t_invalidated_at`` column and returns the correct status code
without duplicating logic.

OpenAPI
-------
FastAPI generates ``openapi.json`` from the actually-registered routes.
In ``rest`` mode the spec contains only verb paths; in ``post_only`` only POST
aliases; in ``both`` both.  SDK generators therefore produce code that matches
the deployed mode.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supported values (strings kept lowercase to match env-var conventions)
# ---------------------------------------------------------------------------

HttpMethodsMode = Literal["rest", "post_only", "both"]
AliasSeperator = Literal["colon", "slash"]

_VALID_MODES: frozenset[str] = frozenset({"rest", "post_only", "both"})
_VALID_SEPS: frozenset[str] = frozenset({"colon", "slash"})

# The canonical source of truth for these defaults is
# ``registry.config.Settings.http_methods_mode`` /
# ``Settings.http_method_alias_separator``. The constants below are
# kept as a fallback for module-load-time consumers (routers that
# import ``get_mode_settings()`` before an app is built); ``from_env``
# in ``config.py`` reads the same env vars so the two paths can't drift.
_DEFAULT_MODE: HttpMethodsMode = "rest"
_DEFAULT_SEP: AliasSeperator = "colon"

# ---------------------------------------------------------------------------
# Environment-variable helpers
# ---------------------------------------------------------------------------


def get_mode_settings() -> tuple[HttpMethodsMode, AliasSeperator]:
    """Read ``REGISTRY_HTTP_METHODS_MODE`` and ``REGISTRY_HTTP_METHOD_ALIAS_SEPARATOR``.

    Returns ``(mode, separator)`` with validated values.  Falls back to
    ``("rest", "colon")`` on missing or invalid env vars and emits a warning.

    The canonical defaults live in :class:`registry.config.Settings`
    (``http_methods_mode`` / ``http_method_alias_separator``). This
    function duplicates the env-var read because routers register routes
    at import time, before any ``Settings`` instance exists. Keep the
    defaults in this module and in ``Settings`` in sync — both read the
    same env-var names so a deployment-level override flows to both.
    """
    # Routers register routes at module-import time before any Settings
    # exists — same defaults as Settings, see the function docstring.
    raw_mode = os.environ.get("REGISTRY_HTTP_METHODS_MODE", _DEFAULT_MODE).strip().lower()  # config: intentional
    raw_sep = (  # config: intentional
        os.environ.get("REGISTRY_HTTP_METHOD_ALIAS_SEPARATOR", _DEFAULT_SEP).strip().lower()
    )

    if raw_mode not in _VALID_MODES:
        _log.warning(
            "REGISTRY_HTTP_METHODS_MODE=%r is not one of %s; falling back to %r",
            raw_mode,
            sorted(_VALID_MODES),
            _DEFAULT_MODE,
        )
        raw_mode = _DEFAULT_MODE

    if raw_sep not in _VALID_SEPS:
        _log.warning(
            "REGISTRY_HTTP_METHOD_ALIAS_SEPARATOR=%r is not one of %s; falling back to %r",
            raw_sep,
            sorted(_VALID_SEPS),
            _DEFAULT_SEP,
        )
        raw_sep = _DEFAULT_SEP

    return raw_mode, raw_sep  # type: ignore[return-value]


def _sep_char(separator: AliasSeperator) -> str:
    """Return the literal separator character for path construction."""
    return ":" if separator == "colon" else "/"


# ---------------------------------------------------------------------------
# HttpMethodRouter
# ---------------------------------------------------------------------------


class HttpMethodRouter:
    """Wrapper around a FastAPI ``APIRouter`` that registers mutation routes
    according to the active ``REGISTRY_HTTP_METHODS_MODE``.

    The same handler callable is registered under both surfaces when
    ``mode='both'``; there is no handler wrapping or delegation.

    Parameters
    ----------
    base_router:
        The ``APIRouter`` instance to register routes on.  The caller includes
        this router in the FastAPI app as normal.
    mode:
        ``"rest"`` | ``"post_only"`` | ``"both"``.
    separator:
        ``"colon"`` | ``"slash"`` — controls the POST-alias path format.
    """

    def __init__(
        self,
        base_router: APIRouter,
        mode: HttpMethodsMode = _DEFAULT_MODE,
        separator: AliasSeperator = _DEFAULT_SEP,
    ) -> None:
        if mode not in _VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(_VALID_MODES)}, got {mode!r}")
        if separator not in _VALID_SEPS:
            raise ValueError(f"separator must be one of {sorted(_VALID_SEPS)}, got {separator!r}")

        self._router = base_router
        self._mode = mode
        self._separator = separator
        self._sep_char = _sep_char(separator)

        _log.debug(
            "HttpMethodRouter initialised mode=%s separator=%s",
            mode,
            separator,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def router(self) -> APIRouter:
        """The underlying ``APIRouter`` — include this in the FastAPI app."""
        return self._router

    @property
    def mode(self) -> HttpMethodsMode:
        return self._mode

    @property
    def separator(self) -> AliasSeperator:
        return self._separator

    def add_mutation_route(
        self,
        path: str,
        action: str,
        handler: Callable[..., Any],
        verb: str,
        **route_kwargs: Any,
    ) -> None:
        """Register *handler* under one or both HTTP surfaces.

        Parameters
        ----------
        path:
            The resource path, e.g. ``"/{entity_id}"``.  Must include any
            prefix that the ``base_router`` does *not* already carry.
        action:
            The mutation verb name (``"update"``, ``"delete"``, ``"replace"``
            etc.).  Used to construct the POST-alias path.
        handler:
            The single handler callable.  It is registered directly — no
            wrapper is created.
        verb:
            The conventional HTTP method (``"PATCH"``, ``"DELETE"``,
            ``"PUT"`` etc.).  Case-insensitive.
        **route_kwargs:
            Extra keyword arguments forwarded to ``APIRouter.add_api_route``
            (``response_model``, ``status_code``, ``response_class``, …).
        """
        verb_upper = verb.upper()
        verb_lower = verb.lower()

        if self._mode in ("rest", "both"):
            self._router.add_api_route(
                path,
                handler,
                methods=[verb_upper],
                **route_kwargs,
            )
            _log.debug(
                "HttpMethodRouter: registered %s %s",
                verb_upper,
                path,
            )

        if self._mode in ("post_only", "both"):
            alias_path = f"{path}{self._sep_char}{action}"
            # POST-tunneled aliases tag an operation_id suffix so OpenAPI does
            # not collide with the verb route when mode='both'.
            tunneled_kwargs = dict(route_kwargs)
            if "operation_id" not in tunneled_kwargs:
                tunneled_kwargs["operation_id"] = (
                    f"tunnel_{verb_lower}_{action}" f"_{path.replace('/', '_').replace('{', '').replace('}', '')}"
                ).strip("_")

            self._router.add_api_route(
                alias_path,
                handler,
                methods=["POST"],
                **tunneled_kwargs,
            )
            _log.debug(
                "HttpMethodRouter: registered POST %s (alias for %s %s)",
                alias_path,
                verb_upper,
                path,
            )

    def add_read_route(
        self,
        path: str,
        handler: Callable[..., Any],
        **route_kwargs: Any,
    ) -> None:
        """Register a GET route (not subject to mode switching — reads are always REST).

        Provided as a convenience so callers can build their entire router
        through ``HttpMethodRouter`` without importing ``APIRouter`` directly.
        """
        self._router.add_api_route(path, handler, methods=["GET"], **route_kwargs)
        _log.debug("HttpMethodRouter: registered GET %s", path)

    def add_create_route(
        self,
        path: str,
        handler: Callable[..., Any],
        **route_kwargs: Any,
    ) -> None:
        """Register a POST create route (not subject to mode switching).

        POST for resource creation is unambiguous and always registered; only
        *mutation* verbs (PATCH, PUT, DELETE) are subject to mode switching.
        """
        self._router.add_api_route(path, handler, methods=["POST"], **route_kwargs)
        _log.debug("HttpMethodRouter: registered POST %s (create)", path)


# ---------------------------------------------------------------------------
# DELETE idempotency helper
# ---------------------------------------------------------------------------


def soft_delete_response_code(
    *,
    found: bool,
    already_invalidated: bool,
    hard_purged: bool = False,
) -> int:
    """Compute the correct HTTP status code for a soft-delete request.

    Encodes the RFC 9110 §9.3.5 idempotency contract without requiring
    callers to duplicate the decision logic.

    Parameters
    ----------
    found:
        True if the row ID exists (even if invalidated).  False if the ID was
        never inserted or has been hard-purged.
    already_invalidated:
        True if the row already has ``t_invalidated_at IS NOT NULL`` before
        this call.  Ignored when ``found=False``.
    hard_purged:
        True if the row was found via its ID but its body / references have
        been crypto-shredded or RTBF-purged.  In this case 404 is returned
        even though the row technically exists in the DB.

    Returns
    -------
    int
        ``204`` for success (first delete or idempotent repeat);
        ``404`` for never-existing or hard-purged IDs.
    """
    if not found or hard_purged:
        return 404
    # Both first-delete and repeat-delete return 204 (idempotent).
    return 204


__all__ = [
    "AliasSeperator",
    "HttpMethodRouter",
    "HttpMethodsMode",
    "get_mode_settings",
    "soft_delete_response_code",
]
