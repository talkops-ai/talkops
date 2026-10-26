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

"""TalkOps — Autonomous CLI-first AI agent for AWS Infrastructure and Applications."""

import logging
import os

# Silence C++ gRPC / Abseil logs (e.g., ev_poll_posix.cc fork FD warnings from google-vertexai)
# that write directly to OS file descriptor 2 and corrupt the Textual TUI screen.
os.environ.setdefault("GRPC_VERBOSITY", "NONE")
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "0")
os.environ.setdefault("ABSL_MIN_LOG_LEVEL", "3")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

from talkops._debug import configure_debug_logging
from talkops._debug_buffer import install_log_buffer
from talkops._version import __version__

_package_logger = logging.getLogger(__name__)
install_log_buffer(_package_logger)
configure_debug_logging(_package_logger)

__all__ = ["__version__"]
