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

"""Version information and lightweight constants for talkops."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__: str = _pkg_version("talkops-ai")
except PackageNotFoundError:
    try:
        __version__ = _pkg_version("talkops")
    except PackageNotFoundError:
        __version__ = "0.1.0-dev"

DOCS_URL = "https://github.com/talkops-ai/talkops"
CHANGELOG_URL = "https://github.com/talkops-ai/talkops/blob/main/CHANGELOG.md"
USER_AGENT = f"talkops/{__version__}"
