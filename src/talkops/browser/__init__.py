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

"""TalkOps browser automation subsystem (Stagehand + 3-tier Chrome persona & subagent architecture)."""

from __future__ import annotations

from talkops.browser.session import (
    DEFAULT_BROWSER_ARTIFACTS_DIR,
    DEFAULT_BROWSER_PROFILE_DIR,
    BrowserConnectionConfig,
    BrowserConnectionError,
    BrowserConnectionMode,
    BrowserSessionManager,
    default_chrome_user_data_dirs,
    discover_devtools_active_port,
    parse_devtools_active_port_file,
)
from talkops.middleware.browser import (
    BROWSER_SAFETY_AND_HANDOFF_SECTION,
    BROWSER_SUBAGENT_SYSTEM_PROMPT,
    CHROME_DEFAULT_PROFILE_SECTION,
    CURRENT_SNAPSHOT_EXPLANATION,
    CURRENT_SNAPSHOT_TAG,
    DEFAULT_MAX_SNAPSHOT_BYTES,
    TalkOpsBrowserMiddleware,
    format_and_offload_snapshot,
    prune_stale_snapshots_in_messages,
    validate_browser_navigation_urls,
)

__all__ = [
    "BROWSER_SAFETY_AND_HANDOFF_SECTION",
    "BROWSER_SUBAGENT_SYSTEM_PROMPT",
    "BrowserConnectionConfig",
    "BrowserConnectionError",
    "BrowserConnectionMode",
    "BrowserSessionManager",
    "CHROME_DEFAULT_PROFILE_SECTION",
    "CURRENT_SNAPSHOT_EXPLANATION",
    "CURRENT_SNAPSHOT_TAG",
    "DEFAULT_BROWSER_ARTIFACTS_DIR",
    "DEFAULT_BROWSER_PROFILE_DIR",
    "DEFAULT_MAX_SNAPSHOT_BYTES",
    "TalkOpsBrowserMiddleware",
    "default_chrome_user_data_dirs",
    "discover_devtools_active_port",
    "format_and_offload_snapshot",
    "parse_devtools_active_port_file",
    "prune_stale_snapshots_in_messages",
    "validate_browser_navigation_urls",
]
