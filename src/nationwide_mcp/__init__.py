"""Unofficial read-only MCP server for Nationwide Building Society (UK) account data."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("nationwide-mcp")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"
