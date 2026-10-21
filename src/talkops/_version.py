"""Version information and lightweight constants for talkops."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

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
