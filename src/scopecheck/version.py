"""One installed package version shared by CLI, API, and worker."""

from importlib.metadata import version

PACKAGE_VERSION = version("scopecheck")
