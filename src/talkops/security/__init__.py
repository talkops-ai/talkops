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

"""TalkOps v2 security primitives: three-layer secret scrubbing and execution trust profiles."""

from talkops.security.execution_profiles import (
    PolicyDecision,
    build_filesystem_permissions_for_profile,
    check_egress_url,
    check_scope_guards,
    evaluate_tool_call_policy,
    inspect_datastore_query,
    register_talkops_execution_harness_profiles,
    resolve_runtime_execution_profile,
)
from talkops.security.redaction import (
    DEVOPS_LANGSMITH_EXTRA_RULES,
    build_talkops_pii_middlewares,
    create_devops_secret_anonymizer,
    detect_devops_secrets_pii,
    scrub_text,
    scrub_tool_payload,
    wrap_env_with_langsmith_secret,
)

__all__ = [
    "DEVOPS_LANGSMITH_EXTRA_RULES",
    "PolicyDecision",
    "build_filesystem_permissions_for_profile",
    "build_talkops_pii_middlewares",
    "check_egress_url",
    "check_scope_guards",
    "create_devops_secret_anonymizer",
    "detect_devops_secrets_pii",
    "evaluate_tool_call_policy",
    "inspect_datastore_query",
    "register_talkops_execution_harness_profiles",
    "resolve_runtime_execution_profile",
    "scrub_text",
    "scrub_tool_payload",
    "wrap_env_with_langsmith_secret",
]
