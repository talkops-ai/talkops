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

"""Security policies, approval modes, and AST command safety evaluators for TalkOps."""

from talkops.security.approval_mode import (
    APPROVAL_MODE_NAMESPACE,
    ApprovalMode,
    approval_mode_key,
    approval_mode_scope,
    coerce_approval_mode,
    get_approval_mode,
    next_approval_mode,
    set_approval_mode,
)
from talkops.security.approval_mode_source import (
    ApprovalPolicyResolver,
    _approval_mode_source,
    _aresolve_approval_mode,
    _DecidedMode,
    _LiveLookup,
    _resolve_approval_mode,
)
from talkops.security.cli_ast_evaluator import (
    DANGEROUS_FLAGS,
    DANGEROUS_VERBS,
    READONLY_UTILITIES,
    READONLY_VERBS,
    WRAPPER_UTILITIES,
    SecurityASTVisitor,
    evaluate_cli_safety,
)
from talkops.security.shell_safety import (
    AWS_DANGEROUS_PATTERNS,
    AWS_SAFE_COMMANDS,
    CLOUD_DANGEROUS_PATTERNS,
    CLOUD_SAFE_COMMANDS,
    DANGEROUS_SHELL_PATTERNS,
    DEVOPS_DESTRUCTIVE_COMMANDS,
    DEVOPS_SAFE_COMMANDS,
    classify_command,
    is_safe_command,
)
from talkops.security.unicode_security import (
    CONFUSABLES,
    UnicodeSanitizeResult,
    normalize_confusables,
    sanitize_unicode,
)
from talkops.security.url_validation import (
    UrlValidationError,
    is_url_safe,
    validate_url,
)

__all__ = [
    "APPROVAL_MODE_NAMESPACE",
    "AWS_DANGEROUS_PATTERNS",
    "AWS_SAFE_COMMANDS",
    "ApprovalMode",
    "ApprovalPolicyResolver",
    "CLOUD_DANGEROUS_PATTERNS",
    "CLOUD_SAFE_COMMANDS",
    "CONFUSABLES",
    "DANGEROUS_FLAGS",
    "DANGEROUS_SHELL_PATTERNS",
    "DANGEROUS_VERBS",
    "DEVOPS_DESTRUCTIVE_COMMANDS",
    "DEVOPS_SAFE_COMMANDS",
    "READONLY_UTILITIES",
    "READONLY_VERBS",
    "SecurityASTVisitor",
    "UnicodeSanitizeResult",
    "UrlValidationError",
    "WRAPPER_UTILITIES",
    "_DecidedMode",
    "_LiveLookup",
    "_approval_mode_source",
    "_aresolve_approval_mode",
    "_resolve_approval_mode",
    "approval_mode_key",
    "approval_mode_scope",
    "classify_command",
    "coerce_approval_mode",
    "evaluate_cli_safety",
    "get_approval_mode",
    "is_safe_command",
    "is_url_safe",
    "next_approval_mode",
    "normalize_confusables",
    "sanitize_unicode",
    "set_approval_mode",
    "validate_url",
]
