# Copyright talkops.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Three-layer secret & PII scrubbing barrier for TalkOps v2.

Layer 1: Pre-span / pre-checkpoint tool payload scrubber (``scrub_tool_payload``)
    Runs inside MCP tool wrappers and ``LocalShellBackend.execute`` / ``aexecute``
    *before* LangChain emits the child tool span or persists ``ToolMessage`` to
    LangGraph checkpoints.
Layer 2: Native LangChain ``PIIMiddleware`` stack (``build_talkops_pii_middlewares``)
    Applies input, tool-result, and model-output wire redaction using LangChain's
    official ``PIIMiddleware`` + a custom DevOps secret detector.
Layer 3: ``langsmith.anonymizer.create_secret_anonymizer`` (``DEVOPS_LANGSMITH_EXTRA_RULES``)
    Provides defense-in-depth trace redaction on the LangSmith ``Client`` before HTTP upload.
"""

from __future__ import annotations

import copy
import json
import os
import re
from collections.abc import Callable, Mapping
from typing import Any

from langchain.agents.middleware import PIIMiddleware
from langchain.agents.middleware.pii import PIIMatch

# ---------------------------------------------------------------------------
# Compiled DevOps / Cloud / Database / Kubernetes Secret Patterns
# ---------------------------------------------------------------------------

_PEM_KEY_RE = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
    r"[\s\S]*?"
    r"-----END (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----",
    re.MULTILINE,
)

_AWS_ACCESS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")

_AWS_SECRET_ASSIGN_RE = re.compile(
    r"(?i)(\b(?:aws_secret_access_key|aws_session_token)\s*[:=]\s*[\"']?)([A-Za-z0-9/+=]{24,})([\"']?)"
)

_GCP_OAUTH_RE = re.compile(r"\bya29\.[0-9A-Za-z\-_]{20,}\b")

_AZURE_SAS_RE = re.compile(r"(?i)(\bsig=)([A-Za-z0-9%+/=]{24,})")

_JWT_BEARER_RE = re.compile(
    r"(?i)(\bBearer\s+)(eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"
)

_RAW_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
)

_API_TOKEN_RE = re.compile(
    r"\b(?:"
    r"ghp_[A-Za-z0-9]{20,}"
    r"|ghs_[A-Za-z0-9]{20,}"
    r"|gho_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{22,}"
    r"|xox[baprs]-[A-Za-z0-9-]{16,}"
    r"|sk-ant-[A-Za-z0-9_-]{20,}"
    r"|sk-proj-[A-Za-z0-9_-]{20,}"
    r"|sk-[A-Za-z0-9]{32,}"
    r"|lsv2_(?:pt|sk)_[A-Za-z0-9_]{20,}"
    r"|glpat-[A-Za-z0-9_-]{16,}"
    r")\b"
)

_CONNECTION_URI_RE = re.compile(
    r"(?i)\b((?:postgres(?:ql)?|mysql|mariadb|redis(?:s)?|mongodb(?:\+srv)?|amqp(?:s)?|kafka)://"
    r"(?:[^:/@\s]+)?):"
    r"([^@\s\"'`]{3,})"
    r"(@"
    r"[^\s\"'`]+)"
)

_ENV_SECRET_ASSIGN_RE = re.compile(
    r"(?im)^(\s*(?:export\s+)?"
    r"[A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|API_KEY|PRIVATE_KEY|CLIENT_SECRET|ACCESS_TOKEN|AUTH_TOKEN|SIGNING_KEY)[A-Z0-9_]*"
    r"\s*=\s*[\"']?)"
    r"([^\s\"'#\r\n]{6,})"
    r"([\"']?)"
)

_JSON_SECRET_FIELD_RE = re.compile(
    r'(?i)("(?:password|passwd|secret|client_secret|private_key|api_key|access_token|refresh_token|session_token|aws_secret_access_key)"\s*:\s*")'
    r'([^"\r\n]{6,})'
    r'(")'
)

# Kubernetes YAML Secret `data:` / `stringData:` block scrubber
_K8S_YAML_KIND_SECRET_RE = re.compile(r"(?im)^\s*kind\s*:\s*Secret\s*$")
_K8S_DATA_SECTION_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)(?P<section>data|stringData)\s*:\s*\n(?P<body>(?:^(?P=indent)[ \t]+[^\n]+\n?)+)"
)
_K8S_DATA_ENTRY_RE = re.compile(
    r"(?m)^(?P<lead>[ \t]+[A-Za-z0-9_.-]+\s*:\s*)(?P<val>[^\s#\r\n][^\r\n]*)$"
)

_SENSITIVE_ENV_KEY_SUBSTRINGS: tuple[str, ...] = (
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "TOKEN",
    "API_KEY",
    "PRIVATE_KEY",
    "AUTHORIZATION",
    "CREDENTIAL",
    "COOKIE",
)

# Extra rules compatible with langsmith.anonymizer.StringNodeRule
DEVOPS_LANGSMITH_EXTRA_RULES: list[dict[str, Any]] = [
    {"pattern": _PEM_KEY_RE, "replace": "[REDACTED:PEM_PRIVATE_KEY]"},
    {"pattern": _AWS_ACCESS_KEY_RE, "replace": "[REDACTED:AWS_ACCESS_KEY]"},
    {"pattern": _GCP_OAUTH_RE, "replace": "[REDACTED:GCP_TOKEN]"},
    {"pattern": _API_TOKEN_RE, "replace": "[REDACTED:API_TOKEN]"},
    {"pattern": _RAW_JWT_RE, "replace": "[REDACTED:JWT_TOKEN]"},
    {"pattern": _CONNECTION_URI_RE, "replace": r"\1:[REDACTED:URI_PASSWORD]\3"},
]


def _scrub_k8s_secret_yaml(text: str) -> str:
    """Scrub base64/plaintext entries inside Kubernetes ``kind: Secret`` YAML manifests."""
    if not _K8S_YAML_KIND_SECRET_RE.search(text):
        return text

    def _replace_section(match: re.Match[str]) -> str:
        indent = match.group("indent")
        section = match.group("section")
        body = match.group("body")

        def _replace_entry(entry_match: re.Match[str]) -> str:
            val = entry_match.group("val").strip()
            if not val or val in ("{}", "[]", "|", ">", "|-", ">-") or val.startswith("[REDACTED"):
                return entry_match.group(0)
            return f"{entry_match.group('lead')}[REDACTED:K8S_SECRET_DATA]"

        scrubbed_body = _K8S_DATA_ENTRY_RE.sub(_replace_entry, body)
        return f"{indent}{section}:\n{scrubbed_body}"

    return _K8S_DATA_SECTION_RE.sub(_replace_section, text)


def _scrub_k8s_secret_dict(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Scrub Kubernetes ``Secret`` JSON/dict structures while preserving metadata & keys."""
    is_k8s_secret = (
        str(mapping.get("kind", "")) == "Secret"
        and ("apiVersion" in mapping or "data" in mapping or "stringData" in mapping)
    )
    out: dict[str, Any] = {}
    for k, v in mapping.items():
        if is_k8s_secret and k in ("data", "stringData") and isinstance(v, Mapping):
            out[k] = {str(dk): "[REDACTED:K8S_SECRET_DATA]" for dk in v}
            continue
        key_upper = str(k).upper()
        if (
            isinstance(v, str)
            and v
            and not v.startswith("[REDACTED")
            and any(tok in key_upper for tok in ("PASSWORD", "PASSWD", "CLIENT_SECRET", "PRIVATE_KEY", "SECRET_ACCESS_KEY", "SESSION_TOKEN"))
        ):
            out[k] = "[REDACTED:SECRET_FIELD]"
            continue
        out[k] = scrub_tool_payload(v)
    return out


def scrub_text(text: str) -> str:
    """Scrub DevOps credentials, PEM keys, tokens, URIs, and K8s Secrets from a string."""
    if not text or len(text) < 8:
        return text

    result = _PEM_KEY_RE.sub("[REDACTED:PEM_PRIVATE_KEY]", text)
    result = _scrub_k8s_secret_yaml(result)

    # Also detect inline JSON Kubernetes Secret objects inside text outputs
    if '"kind"' in result and '"Secret"' in result and ('"data"' in result or '"stringData"' in result):
        try:
            parsed = json.loads(result)
            if isinstance(parsed, Mapping):
                return json.dumps(_scrub_k8s_secret_dict(parsed), indent=2)
        except Exception:
            pass

    result = _CONNECTION_URI_RE.sub(r"\1:[REDACTED:URI_PASSWORD]\3", result)
    result = _AWS_SECRET_ASSIGN_RE.sub(r"\1[REDACTED:AWS_SECRET_KEY]\3", result)
    result = _AWS_ACCESS_KEY_RE.sub("[REDACTED:AWS_ACCESS_KEY]", result)
    result = _GCP_OAUTH_RE.sub("[REDACTED:GCP_TOKEN]", result)
    result = _AZURE_SAS_RE.sub(r"\1[REDACTED:AZURE_SAS_SIG]", result)
    result = _JWT_BEARER_RE.sub(r"\1[REDACTED:JWT_TOKEN]", result)
    result = _RAW_JWT_RE.sub("[REDACTED:JWT_TOKEN]", result)
    result = _API_TOKEN_RE.sub("[REDACTED:API_TOKEN]", result)
    result = _ENV_SECRET_ASSIGN_RE.sub(r"\1[REDACTED:ENV_SECRET]\3", result)
    result = _JSON_SECRET_FIELD_RE.sub(r"\1[REDACTED:SECRET_FIELD]\3", result)
    return result


def scrub_tool_payload(value: Any, *, tool_name: str | None = None) -> Any:
    """Recursively scrub tool return payloads before LangSmith spans or checkpoint persistence."""
    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, str):
        return scrub_text(value)

    if isinstance(value, bytes):
        try:
            decoded = value.decode("utf-8")
            scrubbed = scrub_text(decoded)
            return scrubbed.encode("utf-8") if scrubbed != decoded else value
        except UnicodeDecodeError:
            return value

    if isinstance(value, tuple):
        return tuple(scrub_tool_payload(item, tool_name=tool_name) for item in value)

    if isinstance(value, list):
        return [scrub_tool_payload(item, tool_name=tool_name) for item in value]

    if isinstance(value, Mapping):
        return _scrub_k8s_secret_dict(value)

    # DeepAgents ExecuteResponse (output: str, exit_code: int | None, truncated: bool)
    if hasattr(value, "output") and hasattr(value, "exit_code"):
        raw_out = getattr(value, "output", None)
        if isinstance(raw_out, str):
            scrubbed_out = scrub_text(raw_out)
            if scrubbed_out != raw_out:
                try:
                    cloned = copy.copy(value)
                    cloned.output = scrubbed_out
                    return cloned
                except Exception:
                    return type(value)(
                        output=scrubbed_out,
                        exit_code=getattr(value, "exit_code", None),
                        truncated=bool(getattr(value, "truncated", False)),
                    )
        return value

    # LangChain ToolMessage or AIMessage
    if hasattr(value, "content") and hasattr(value, "model_copy"):
        updates: dict[str, Any] = {}
        raw_content = getattr(value, "content", None)
        scrubbed_content = scrub_tool_payload(raw_content, tool_name=tool_name)
        if scrubbed_content != raw_content:
            updates["content"] = scrubbed_content
        if hasattr(value, "artifact") and getattr(value, "artifact", None) is not None:
            raw_artifact = getattr(value, "artifact")
            scrubbed_artifact = scrub_tool_payload(raw_artifact, tool_name=tool_name)
            if scrubbed_artifact != raw_artifact:
                updates["artifact"] = scrubbed_artifact
        if updates:
            return value.model_copy(update=updates)
        return value

    return value


def detect_devops_secrets_pii(content: str) -> list[PIIMatch]:
    """Custom detector for LangChain ``PIIMiddleware`` covering cloud, DB, and K8s secrets."""
    if not content or len(content) < 8:
        return []

    matches: list[PIIMatch] = []
    detector_specs: tuple[tuple[str, re.Pattern[str]], ...] = (
        ("pem_private_key", _PEM_KEY_RE),
        ("aws_access_key", _AWS_ACCESS_KEY_RE),
        ("gcp_token", _GCP_OAUTH_RE),
        ("api_token", _API_TOKEN_RE),
        ("jwt_token", _RAW_JWT_RE),
        ("connection_uri", _CONNECTION_URI_RE),
    )

    occupied_spans: list[tuple[int, int]] = []

    for secret_type, pattern in detector_specs:
        for m in pattern.finditer(content):
            start, end = m.span()
            if any(not (end <= s or start >= e) for s, e in occupied_spans):
                continue
            occupied_spans.append((start, end))
            matches.append(
                PIIMatch(
                    type=secret_type,
                    value=m.group(0),
                    start=start,
                    end=end,
                )
            )

    matches.sort(key=lambda item: item["start"])
    return matches


def build_talkops_pii_middlewares(*, enabled: bool | None = None) -> list[PIIMiddleware]:
    """Build the native LangChain ``PIIMiddleware`` barrier for TalkOps agents.

    Applies input, tool-result, and model-output wire redaction by default unless
    explicitly disabled via ``TALKOPS_DISABLE_PII_REDACTION=true``.
    """
    if enabled is None:
        disabled = os.environ.get("TALKOPS_DISABLE_PII_REDACTION", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        enabled = not disabled
    if not enabled:
        return []

    return [
        PIIMiddleware(
            "devops_secret",
            strategy="redact",
            detector=detect_devops_secrets_pii,
            apply_to_input=True,
            apply_to_output=True,
            apply_to_tool_results=True,
        ),
        PIIMiddleware(
            "credit_card",
            strategy="mask",
            apply_to_input=True,
            apply_to_output=True,
            apply_to_tool_results=True,
        ),
    ]


def wrap_env_with_langsmith_secret(env_mapping: Mapping[str, str]) -> dict[str, Any]:
    """Wrap sensitive environment/header values in ``LangSmithSecret`` for trace masking."""
    try:
        from langsmith.anonymizer import LangSmithSecret
    except ImportError:
        return dict(env_mapping)

    wrapped: dict[str, Any] = {}
    for k, v in env_mapping.items():
        key_upper = k.upper()
        if isinstance(v, str) and v and any(sub in key_upper for sub in _SENSITIVE_ENV_KEY_SUBSTRINGS):
            wrapped[k] = LangSmithSecret(v)
        else:
            wrapped[k] = v
    return wrapped


def create_devops_secret_anonymizer() -> Callable[[Any], Any] | None:
    """Create a ``langsmith.anonymizer.create_secret_anonymizer`` callable with DevOps rules."""
    try:
        from langsmith.anonymizer import create_secret_anonymizer

        return create_secret_anonymizer(
            extra_rules=DEVOPS_LANGSMITH_EXTRA_RULES,  # type: ignore[arg-type]
            max_depth=24,
        )
    except Exception:
        return None
