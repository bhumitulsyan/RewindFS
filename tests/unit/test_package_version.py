"""Package metadata/version consistency tests."""

from importlib.metadata import PackageNotFoundError, version

import rewindfs


def test_dunder_version_matches_installed_distribution() -> None:
    """`rewindfs.__version__` should reflect installed distribution version."""
    try:
        dist_version = version("rewindfs")
    except PackageNotFoundError:
        assert rewindfs.__version__ == "0.0.0+unknown"
    else:
        assert rewindfs.__version__ == dist_version
