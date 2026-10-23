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

"""Exception hierarchy for talkops."""

from __future__ import annotations


class TalkOpsError(Exception):
    """Base exception for all talkops errors."""


class ServerStartupError(TalkOpsError):
    """Raised when the LangGraph dev server fails to start or become healthy."""


class ModelConfigError(TalkOpsError):
    """Model configuration is invalid or incomplete."""


class MissingCredentialsError(ModelConfigError):
    """Required API credentials are not configured."""

    def __init__(self, message: str, *, provider: str = "", env_var: str | None = None) -> None:
        """Initialize MissingCredentialsError."""
        super().__init__(message)
        self.provider = provider
        self.env_var = env_var


class MissingProviderPackageError(ModelConfigError):
    """Required LangChain provider package is not installed."""

    def __init__(self, message: str, *, provider: str = "", package: str = "") -> None:
        """Initialize MissingProviderPackageError."""
        super().__init__(message)
        self.provider = provider
        self.package = package


class NoCredentialsConfiguredError(MissingCredentialsError):
    """No credentials configured for any auto-detectable provider."""

    def __init__(self, message: str) -> None:
        """Initialize NoCredentialsConfiguredError."""
        super().__init__(message, provider="", env_var=None)


class SkillExecutionError(TalkOpsError):
    """Raised when skill loading or execution fails."""


class PluginError(TalkOpsError):
    """Raised when plugin resolution or installation fails."""


class DestructiveActionRejectedError(TalkOpsError):
    """Raised when a human rejects a gated destructive cloud action."""


__all__ = [
    "TalkOpsError",
    "ServerStartupError",
    "ModelConfigError",
    "MissingCredentialsError",
    "MissingProviderPackageError",
    "NoCredentialsConfiguredError",
    "SkillExecutionError",
    "PluginError",
    "DestructiveActionRejectedError",
]
