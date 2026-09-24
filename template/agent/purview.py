"""Agent-scoped Purview checks at content boundaries, before releasing data."""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

import httpx
from opentelemetry import trace

log = logging.getLogger(__name__)
GRAPH = "https://graph.microsoft.com/v1.0"
MAX_CONTENT_BYTES = 64 * 1024


class PurviewBlockedError(RuntimeError):
    """The policy denied the content; the original content must not be released."""


class PurviewUnavailableError(RuntimeError):
    """A required evaluation could not be completed; this is not permission."""


class PurviewPolicyClient:
    def __init__(self, *, agent_user_id: str, application_id: str, agent_id: str,
                 blueprint_id: str, agent_name: str, token_provider: Callable[[], str],
                 client: httpx.Client | None = None, cache_seconds: float = 300,
                 check_output: bool = False):
        self.agent_user_id = str(uuid.UUID(agent_user_id))
        self.application_id = str(uuid.UUID(application_id))
        self.agent_id = str(uuid.UUID(agent_id))
        self.blueprint_id = str(uuid.UUID(blueprint_id))
        if self.agent_user_id in (self.agent_id, self.application_id, self.blueprint_id):
            raise ValueError("Purview requires an Agent User ID, not an application or agent identity ID.")
        if not agent_name or not 0 < cache_seconds <= 3600:
            raise ValueError("Invalid Purview agent name or policy cache lifetime.")
        self.agent_name = agent_name
        self.check_output = check_output
        self._token_provider = token_provider
        self._client = client or httpx.Client(timeout=15.0, follow_redirects=False)
        self._owns_client = client is None
        self._cache_seconds = cache_seconds
        self._cache = None
        self._lock = threading.RLock()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _post(self, action: str, body: dict, etag: str | None = None) -> tuple[dict, str | None]:
        try:
            token = self._token_provider()
            if not isinstance(token, str) or not token:
                raise PurviewUnavailableError("PURVIEW_TOKEN_UNAVAILABLE")
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Prefer": "include-unknown-enum-members",
            }
            if etag:
                headers["If-None-Match"] = etag
            response = self._client.post(
                f"{GRAPH}/users/{self.agent_user_id}/dataSecurityAndGovernance/{action}",
                json=body, headers=headers, timeout=15.0, follow_redirects=False)
            if response.status_code != 200:
                raise PurviewUnavailableError(f"PURVIEW_HTTP_{response.status_code}")
            data = response.json()
            if not isinstance(data, dict):
                raise PurviewUnavailableError("PURVIEW_INVALID_RESPONSE")
            return data, response.headers.get("ETag")
        except PurviewUnavailableError:
            raise
        except Exception:
            raise PurviewUnavailableError("PURVIEW_REQUEST_FAILED") from None

    @staticmethod
    def _enforce_actions(actions) -> bool:
        if not isinstance(actions, list):
            raise PurviewUnavailableError("PURVIEW_INVALID_ACTIONS")
        for action in actions:
            if (isinstance(action, dict) and action.get("action") == "restrictAccess"
                    and action.get("restrictionAction") == "block"):
                raise PurviewBlockedError("PURVIEW_DLP_BLOCKED")
        for action in actions:
            if not isinstance(action, dict) or action.get("action") != "restrictAccess":
                raise PurviewUnavailableError("PURVIEW_UNSUPPORTED_ACTION")
            restriction = action.get("restrictionAction")
            if restriction == "warn":
                raise PurviewBlockedError("PURVIEW_CONFIRMATION_REQUIRED")
            if restriction != "audit":
                raise PurviewUnavailableError("PURVIEW_UNSUPPORTED_RESTRICTION")
        return bool(actions)

    def _protection_scopes(self) -> tuple[list, str]:
        with self._lock:
            if self._cache is not None and time.monotonic() < self._cache[2]:
                return self._cache[0], self._cache[1]
            body, etag = self._post("protectionScopes/compute", {
                "activities": "uploadText,downloadText",
                "locations": [{
                    "@odata.type": "microsoft.graph.policyLocationApplication",
                    "value": self.application_id,
                }],
            })
            scopes = body.get("value")
            if not isinstance(scopes, list) or not isinstance(etag, str) or not etag.strip():
                raise PurviewUnavailableError("PURVIEW_INVALID_SCOPES")
            self._cache = (scopes, etag, time.monotonic() + self._cache_seconds)
            return scopes, etag

    def _require_inline_scope(self, scopes: list, activity: str) -> bool:
        inline = False
        audited = False
        for scope in scopes:
            if not isinstance(scope, dict) or not isinstance(scope.get("activities"), str):
                raise PurviewUnavailableError("PURVIEW_INVALID_SCOPE")
            if activity not in {value.strip() for value in scope["activities"].split(",")}:
                continue
            locations = scope.get("locations")
            if not isinstance(locations, list) or not locations:
                raise PurviewUnavailableError("PURVIEW_INVALID_LOCATIONS")
            applicable = False
            for location in locations:
                if not isinstance(location, dict):
                    raise PurviewUnavailableError("PURVIEW_INVALID_LOCATION")
                location_type = location.get("@odata.type", "microsoft.graph.policyLocationApplication")
                if not isinstance(location_type, str):
                    raise PurviewUnavailableError("PURVIEW_INVALID_LOCATION")
                location_type = location_type.lstrip("#")
                value = location.get("value")
                if (location_type == "microsoft.graph.policyLocationApplication"
                        and isinstance(value, str) and value.lower() == self.application_id):
                    applicable = True
            if not applicable:
                continue
            scope_audit = self._enforce_actions(scope.get("policyActions"))
            audited = audited or scope_audit
            mode = scope.get("executionMode")
            if mode not in ("evaluateInline", "evaluateOffline"):
                raise PurviewUnavailableError("PURVIEW_UNSUPPORTED_EXECUTION_MODE")
            inline = inline or mode == "evaluateInline"
        if not inline:
            raise PurviewUnavailableError("PURVIEW_INLINE_POLICY_REQUIRED")
        return audited

    def check_text(self, content: str, *, activity: str, checkpoint: str,
                   correlation_id: str, sequence_number: int) -> None:
        if checkpoint not in ("prompt", "tool_response", "agent_response"):
            raise ValueError("Unknown Purview checkpoint.")
        expected_activity = "downloadText" if checkpoint == "agent_response" else "uploadText"
        if activity != expected_activity:
            raise ValueError("Activity does not match the content boundary.")
        try:
            if not isinstance(content, str) or not content or len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
                raise PurviewUnavailableError("PURVIEW_CONTENT_SIZE_UNSUPPORTED")
            if (not isinstance(correlation_id, str) or not correlation_id
                    or type(sequence_number) is not int or sequence_number < 0):
                raise PurviewUnavailableError("PURVIEW_INVALID_CONVERSATION_CONTEXT")
            timestamp = datetime.now(timezone.utc).isoformat()
            body = {"contentToProcess": {
                "contentEntries": [{
                    "@odata.type": "microsoft.graph.processConversationMetadata",
                    "identifier": str(uuid.uuid4()),
                    "content": {"@odata.type": "microsoft.graph.textContent", "data": content},
                    "name": checkpoint,
                    "correlationId": correlation_id,
                    "sequenceNumber": sequence_number,
                    "isTruncated": False,
                    "createdDateTime": timestamp,
                    "modifiedDateTime": timestamp,
                    "agents": [{
                        "@odata.type": "microsoft.graph.aiAgentInfo",
                        "identifier": self.agent_id,
                        "blueprintId": self.blueprint_id,
                        "name": self.agent_name,
                        "version": "1.0",
                    }],
                }],
                "activityMetadata": {"activity": activity},
                "protectedAppMetadata": {
                    "name": self.agent_name,
                    "version": "1.0",
                    "applicationLocation": {
                        "@odata.type": "microsoft.graph.policyLocationApplication",
                        "value": self.application_id,
                    },
                },
                "integratedAppMetadata": {"name": "Agent Factory", "version": "1.0"},
            }}
            scopes, etag = self._protection_scopes()
            scope_audit = self._require_inline_scope(scopes, activity)
            response, _ = self._post("processContent", body, etag)
            state = response.get("protectionScopeState")
            if state == "modified":
                with self._lock:
                    self._cache = None
            errors = response.get("processingErrors")
            if not isinstance(errors, list) or errors:
                raise PurviewUnavailableError("PURVIEW_PROCESSING_ERROR")
            content_audit = self._enforce_actions(response.get("policyActions"))
            if state == "modified":
                refreshed_scopes, _ = self._protection_scopes()
                refreshed_audit = self._require_inline_scope(refreshed_scopes, activity)
                scope_audit = scope_audit or refreshed_audit
            elif state != "notModified":
                raise PurviewUnavailableError("PURVIEW_INVALID_SCOPE_STATE")
            self._record(checkpoint, "audited" if scope_audit or content_audit else "allowed")
        except PurviewBlockedError:
            self._record(checkpoint, "blocked")
            raise
        except PurviewUnavailableError as error:
            self._record(checkpoint, "unavailable", str(error))
            raise

    @staticmethod
    def _record(checkpoint: str, result: str, reason: str = "none") -> None:
        trace.get_current_span().set_attribute(f"purview.{checkpoint}.result", result)
        log.info("Purview evaluation: checkpoint=%s result=%s reason=%s", checkpoint, result, reason)


def configure(manifest: dict) -> PurviewPolicyClient | None:
    enabled = os.getenv("PURVIEW_ENABLED", "false").lower()
    if enabled not in ("true", "false"):
        raise ValueError("PURVIEW_ENABLED must be true or false.")
    if enabled == "false":
        return None
    output_enabled = os.getenv("PURVIEW_CHECK_OUTPUT", "false").lower()
    if output_enabled not in ("true", "false"):
        raise ValueError("PURVIEW_CHECK_OUTPUT must be true or false.")
    required = (
        "A365_TENANT_ID", "A365_AGENT_INSTANCE_ID", "A365_BLUEPRINT_CLIENT_ID",
        "A365_BLUEPRINT_CLIENT_SECRET", "PURVIEW_AGENT_USER_ID",
    )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise ValueError("Purview enabled without required configuration: " + ", ".join(missing))
    from observability import A365TokenService

    tenant_id = str(uuid.UUID(os.environ["A365_TENANT_ID"]))
    token_service = A365TokenService(
        tenant_id, os.environ["A365_BLUEPRINT_CLIENT_ID"],
        os.environ["A365_BLUEPRINT_CLIENT_SECRET"], os.environ["A365_AGENT_INSTANCE_ID"],
        resource_scope="https://graph.microsoft.com/.default")
    return PurviewPolicyClient(
        agent_user_id=os.environ["PURVIEW_AGENT_USER_ID"],
        application_id=os.getenv("PURVIEW_APPLICATION_ID") or os.environ["A365_AGENT_INSTANCE_ID"],
        agent_id=os.environ["A365_AGENT_INSTANCE_ID"],
        blueprint_id=os.environ["A365_BLUEPRINT_CLIENT_ID"],
        agent_name=manifest["displayName"], token_provider=token_service.get_token,
        check_output=output_enabled == "true")