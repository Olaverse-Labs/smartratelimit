"""The version has to agree with itself everywhere it is written down.

The publish workflow already refuses a tag that disagrees with
``pyproject.toml`` — but that check runs *after* the tag is pushed, which is a
late and awkward place to find out. These run on every commit.
"""

import re
from pathlib import Path

import pytest

import smartratelimit

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def declared_version():
    """The version in pyproject.toml, which is what gets published."""
    # Read it by hand rather than with tomllib: this suite runs on 3.8, and the
    # field is unambiguous enough that a parser buys nothing.
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    assert match, "pyproject.toml has no top-level version"
    return match.group(1)


def test_package_version_matches_pyproject(declared_version):
    # __version__ is exported and read by the docs hook, so a lagging value
    # publishes a release that misreports itself.
    assert smartratelimit.__version__ == declared_version


def test_version_is_semver(declared_version):
    assert re.fullmatch(r"\d+\.\d+\.\d+", declared_version), declared_version


def test_changelog_has_an_entry_for_this_version(declared_version):
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{declared_version}]" in changelog, (
        f"CHANGELOG.md has no section for {declared_version}. Bumping the "
        f"version without saying what changed is how a release goes out unread."
    )


def test_docs_changelog_has_an_entry_for_this_version(declared_version):
    docs = (ROOT / "docs" / "changelog.md").read_text(encoding="utf-8")
    assert f"## {declared_version}" in docs
