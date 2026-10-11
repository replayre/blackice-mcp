from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("blackice-mcp")
except PackageNotFoundError:
    __version__ = "unknown"

try:
    from ._build import COMMIT as __commit__
except ImportError:
    __commit__ = "unknown"
