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

"""Types and enums for TalkOps prompts module."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from talkops.utils.logger import get_logger

logger = get_logger(__name__)


class CloudProvider(StrEnum):
    """Supported cloud providers for TalkOps operations and prompts."""

    AWS = "aws"
    AZURE = "azure"
    GCP = "gcp"
    MULTI = "multi"


FsToolName = Literal[
    "read_file",
    "write_file",
    "edit_file",
    "list_dir",
    "grep_search",
    "find_by_name",
]


def normalize_cloud_provider(val: str | CloudProvider | None) -> CloudProvider:
    """Normalize a cloud provider string or enum into a validated CloudProvider.

    Defaults to `CloudProvider.AWS` when unspecified.
    """
    if val is None:
        return CloudProvider.AWS

    if isinstance(val, CloudProvider):
        return val

    normalized = val.strip().lower()
    if normalized in {"aws", "amazon", "amazon_aws"}:
        return CloudProvider.AWS
    if normalized in {"azure", "az", "microsoft", "azure_cloud"}:
        return CloudProvider.AZURE
    if normalized in {"gcp", "google", "google_cloud"}:
        return CloudProvider.GCP
    if normalized in {"multi", "multi_cloud", "multicloud", "hybrid", "cross"}:
        return CloudProvider.MULTI

    logger.warning("Unrecognized cloud provider '%s'; defaulting to AWS.", val)
    return CloudProvider.AWS
