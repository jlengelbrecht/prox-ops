"""A version in a tag or chart comes from the PR, so it can be any length and must never raise."""

import pytest

import render

# int() refuses more than 4300 digits, and a number past nine digits is not one to compare anyway.
HUGE = "9" * 5000


@pytest.mark.parametrize("tag", [f"1.{HUGE}", f"{HUGE}.1", f"v1.{HUGE}.3", "1." + "9" * 10, "1234567890.1"])
def test_a_version_with_a_huge_number_is_not_comparable_and_does_not_raise(tag):
    assert render._version(tag) is None
    assert render._release_order(f"{tag}.0") is None and render._release_order(f"1.2.{HUGE}") is None


@pytest.mark.parametrize("tag,expected", [("1.2", (1, 2)), ("v1.20.3", (1, 20)), ("0.0", (0, 0)),
                                         ("123456789.987654321", (123456789, 987654321)),
                                         ("1.2rc1", (1, 2)), ("1.2-beta", (1, 2))])
def test_ordinary_versions_still_parse(tag, expected):
    assert render._version(tag) == expected


@pytest.mark.parametrize("tag,expected", [("1.2.3", (1, 2, 3, True)), ("v0.20.3-rc.1", (0, 20, 3, False)),
                                         ("999999999.999999999.999999999", (999999999, 999999999, 999999999, True))])
def test_ordinary_release_tags_still_order(tag, expected):
    assert render._release_order(tag) == expected


def test_nine_digits_is_the_limit():
    assert render._version("1." + "9" * 9) == (1, 999999999) and render._version("1." + "9" * 10) is None
    assert render._release_order("1.2." + "9" * 9) is not None and render._release_order("1.2." + "9" * 10) is None


@pytest.mark.parametrize("old,new", [(f"1.{HUGE}", "1.3"), ("1.2", f"1.{HUGE}"), (f"{HUGE}.1", "1.1")])
def test_a_huge_image_tag_is_needs_human_not_an_error(old, new):
    assert render._image_flag({}, {}, f"registry.example/o/app:{old}", f"registry.example/o/app:{new}") == "version"


def test_a_huge_patch_number_is_still_a_patch_change_and_does_not_raise():
    # Only major and minor are compared; the patch part was never parsed.
    assert render._image_flag({}, {}, "registry.example/o/app:1.2.3", f"registry.example/o/app:1.2.{HUGE}") is None


def test_a_huge_release_tag_in_an_excepted_oci_repository_is_needs_human_not_an_error():
    rule = {"tag_pattern": r"v?[0-9.]+(-[A-Za-z0-9.]+)?"}
    assert render._release_flag(rule, ("spec", "ref", "tag"), "1.2.3", f"1.2.{HUGE}") == "version"
    assert render._release_flag(rule, ("spec", "ref", "tag"), f"1.2.{HUGE}", "1.2.3") == "version"


def test_a_chart_version_with_a_huge_number_is_flagged_not_raised():
    before = {"kind": "HelmRelease", "spec": {"chart": {"spec": {"version": "1.2.3"}}}}
    after = {"kind": "HelmRelease", "spec": {"chart": {"spec": {"version": f"1.{HUGE}"}}}}
    assert render.object_flags("HelmRelease", before, after) == ["version"]
