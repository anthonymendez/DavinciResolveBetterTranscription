"""Resolve version / edition detection (better_captions_plugin.compat)."""

import pytest

from better_captions_plugin import compat


@pytest.mark.parametrize(
    "value, expected",
    [
        ([21, 1, 2, 5, ""], (21, 1, 2, 5)),
        ((20, 3, 0, 23, "b"), (20, 3, 0, 23)),
        ({1: 21, 2: 0, 3: 4, 4: 9, 5: ""}, (21, 0, 4, 9)),
        ([21.0, 1.0, 2.0, 5.0], (21, 1, 2, 5)),
        ("21.1.2.0005", (21, 1, 2, 5)),
        ("21.1.2 Build 5", (21, 1, 2, 5)),
        ("19.0", (19, 0)),
        ("", ()),
        (None, ()),
        ("beta", ()),
    ],
)
def test_parse_version(value, expected):
    assert compat.parse_version(value) == expected


class _Resolve:
    def __init__(self, product, version=None, version_string=None):
        self.product, self.version, self.version_string = product, version, version_string

    def GetProductName(self):
        return self.product

    def GetVersion(self):
        if self.version is None:
            raise AttributeError("GetVersion")
        return self.version

    def GetVersionString(self):
        return self.version_string


@pytest.mark.parametrize(
    "product, version, gated",
    [
        ("DaVinci Resolve", [21, 1, 2, 5, ""], True),
        ("DaVinci Resolve", [21, 2, 0, 1, ""], True),
        ("DaVinci Resolve", [22, 0, 0, 1, ""], True),
        ("DaVinci Resolve", [21, 0, 4, 9, ""], False),
        ("DaVinci Resolve", [20, 3, 0, 23, ""], False),
        ("DaVinci Resolve Studio", [21, 1, 2, 5, ""], False),
    ],
)
def test_python_gate_is_free_edition_from_21_1(product, version, gated):
    info = compat.detect(_Resolve(product, version))
    assert info.python_gated is gated
    assert info.studio is ("Studio" in product)


def test_detect_falls_back_to_version_string_and_tolerates_failures():
    info = compat.detect(_Resolve("DaVinci Resolve Studio", None, "21.1.2.0005"))
    assert info.version == (21, 1, 2, 5) and info.label == "DaVinci Resolve Studio 21.1.2"
    assert compat.detect(None) == compat.ResolveInfo(version=())
    unknown = compat.detect(_Resolve("DaVinci Resolve", None, None))
    assert unknown.version == () and not unknown.python_gated and unknown.version_text == "unknown version"


def test_warns_only_for_versions_newer_than_tested():
    assert compat.warnings_for(compat.detect(_Resolve("DaVinci Resolve Studio", [21, 1, 2, 5, ""]))) == []
    (warning,) = compat.warnings_for(compat.detect(_Resolve("DaVinci Resolve Studio", [22, 0, 0, 1, ""])))
    assert "22.0.0" in warning and "21.1" in warning
