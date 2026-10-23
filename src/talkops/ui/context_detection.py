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

"""Production-context detection shared by the status bar and infra panel.

One definition of "production": a name counts when ``prod``, ``production``
or ``prd`` appears as a whole token, delimited by the start/end of the
string or one of ``- _ . / : @`` or a space. That lights ``client-prod``,
``acme_production`` and ``prd-eu`` but not ``product-dev``, ``reproduce``
or ``preprod-test``.

Git branches are deliberately not consulted: ``main`` is not production,
and a branch like ``prod-fix`` would raise a false alarm.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

ProductionSource = Literal["aws", "azure", "gcp", "k8s", "tf"]

_PROD_RE = re.compile(r"(?:^|[-_./:@ ])(prod|production|prd)(?:$|[-_./:@ ])", re.IGNORECASE)

_CURRENT_CONTEXT_RE = re.compile(r"^current-context:\s*(.*?)\s*$", re.MULTILINE)


def is_production_name(value: str | None) -> bool:
    """Whether *value* names a production environment.

    Args:
        value: A profile, context or workspace name; ``None`` / empty is never prod.
    """
    return bool(value) and _PROD_RE.search(value) is not None


def detect_production(
    *,
    aws_profile: str = "",
    azure_subscription: str = "",
    gcp_project: str = "",
    k8s_context: str = "",
    tf_workspace: str = "",
) -> ProductionSource | None:
    """Return which source flagged production (``'aws'``, ``'azure'``, ``'gcp'``, ``'k8s'``, ``'tf'``) or ``None``.

    Sources are checked in that order, so the active cloud profile wins when several match.
    """
    if is_production_name(aws_profile):
        return "aws"
    if is_production_name(azure_subscription):
        return "azure"
    if is_production_name(gcp_project):
        return "gcp"
    if is_production_name(k8s_context):
        return "k8s"
    if is_production_name(tf_workspace):
        return "tf"
    return None


def _kubeconfig_path() -> Path:
    """First file in ``$KUBECONFIG``, else ``~/.kube/config``."""
    env = os.environ.get("KUBECONFIG", "")
    first = next((p for p in env.split(os.pathsep) if p), "")
    return Path(first).expanduser() if first else Path.home() / ".kube" / "config"


def current_k8s_context() -> str:
    """Read ``current-context`` from the kubeconfig without spawning ``kubectl``.

    Honours ``$KUBECONFIG_CONTEXT`` first (as the infra panel always has).
    Returns ``""`` when there is no kubeconfig or no current context.
    """
    override = os.environ.get("KUBECONFIG_CONTEXT", "")
    if override:
        return override
    try:
        text = _kubeconfig_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = _CURRENT_CONTEXT_RE.search(text)
    if not match:
        return ""
    return match.group(1).strip("\"'")


__all__ = [
    "ProductionSource",
    "current_k8s_context",
    "detect_production",
    "is_production_name",
]
