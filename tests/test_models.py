"""merge_values and has_metadata_absent_from: what a merge carries, and what G_METADATA
reports as at risk. sceneMerge copies nothing itself, so anything left out here is lost
when the losing scene is destroyed."""

from conftest import make_scene

from models import merge_values

STASHDB = "https://stashdb.org/graphql"
TPDB = "https://theporndb.net/graphql"


def identified_scene(scene_id="20", **kwargs):
    defaults = dict(
        title="Real Title", studio_id="s1", code="ABC-123", date="2024-05-01",
        details="Synopsis.", director="Someone", stash_ids=[(STASHDB, "aaa")],
    )
    return make_scene(scene_id, **{**defaults, **kwargs})


# Identified loser, unidentified keeper: the codec-first case that motivated the change

def test_merge_values_identified_loser_overrides_unidentified_keepers_identity():
    keeper = make_scene("10", title="file_hevc_1080p")
    loser = identified_scene("20")

    values = merge_values(keeper, [loser])

    assert values["title"] == "Real Title"
    assert values["studio_id"] == "s1"
    assert values["code"] == "ABC-123"
    assert values["date"] == "2024-05-01"
    assert values["details"] == "Synopsis."
    assert values["director"] == "Someone"
    assert values["stash_ids"] == [{"endpoint": STASHDB, "stash_id": "aaa"}]


def test_merge_values_identified_loser_carries_organized():
    keeper = make_scene("10", title="file")
    loser = identified_scene("20", organized=True)

    assert merge_values(keeper, [loser])["organized"] is True


def test_merge_values_identified_loser_omits_fields_it_does_not_have():
    """An empty authority field must not blank the keeper's value."""
    keeper = make_scene("10", title="file", director="Keeper Director")
    loser = identified_scene("20", director="")

    values = merge_values(keeper, [loser])

    assert "director" not in values
    assert values["title"] == "Real Title"


def test_merge_values_omits_fields_already_equal_to_the_authority():
    keeper = make_scene("10", title="Real Title", studio_id="s1")
    loser = identified_scene("20")

    values = merge_values(keeper, [loser])

    assert "title" not in values
    assert "studio_id" not in values


# Both identified: no authority, so scalars only fill gaps

def test_merge_values_both_identified_keeps_the_keepers_scalars():
    keeper = identified_scene("10", title="Keeper Title", studio_id="s9", code="",
                              stash_ids=[(STASHDB, "aaa")])
    loser = identified_scene("20", title="Loser Title", studio_id="s1", code="ABC-123",
                             stash_ids=[(STASHDB, "aaa")])

    values = merge_values(keeper, [loser])

    assert "title" not in values
    assert "studio_id" not in values
    assert values["code"] == "ABC-123"  # keeper had none, so the gap is filled
    assert "stash_ids" not in values  # same link already on the keeper
    assert "organized" not in values


def test_merge_values_both_identified_unions_links_on_different_endpoints():
    keeper = identified_scene("10", stash_ids=[(STASHDB, "aaa")])
    loser = identified_scene("20", stash_ids=[(TPDB, "zzz")])

    values = merge_values(keeper, [loser])

    assert values["stash_ids"] == [
        {"endpoint": STASHDB, "stash_id": "aaa"},
        {"endpoint": TPDB, "stash_id": "zzz"},
    ]


# Conflict: neither side identified, both carry a value

def test_merge_values_conflicting_scalars_without_authority_keep_the_keeper():
    keeper = make_scene("10", title="Keeper Title", date="2020-01-01")
    loser = make_scene("20", title="Loser Title", date="2021-02-02", director="D")

    values = merge_values(keeper, [loser])

    assert "title" not in values
    assert "date" not in values
    assert values["director"] == "D"


def test_merge_values_unidentified_loser_never_overrides():
    keeper = make_scene("10", title="Keeper Title", studio_id="s9")
    loser = make_scene("20", title="Loser Title", studio_id="s1")

    values = merge_values(keeper, [loser])

    assert values == {"id": "10"}


def test_merge_values_identified_keeper_is_not_overridden_by_unidentified_loser():
    keeper = identified_scene("10", title="Keeper Title")
    loser = make_scene("20", title="Loser Title", studio_id="s1", code="X")

    values = merge_values(keeper, [loser])

    assert "title" not in values
    assert "studio_id" not in values
    assert "code" not in values


# has_metadata_absent_from: what G_METADATA reports

def test_identified_loser_reports_its_identity_as_at_risk():
    keeper = make_scene("10", title="file")
    loser = identified_scene("20")

    lost = loser.has_metadata_absent_from(keeper)

    assert lost["stash_ids"] == [f"{STASHDB}#aaa"]
    assert lost["title"] == "Real Title"
    assert lost["studio_id"] == "s1"


def test_unidentified_loser_with_a_different_title_loses_nothing():
    """Otherwise every pair of filename-titled scenes would require a merge."""
    keeper = make_scene("10", title="a")
    loser = make_scene("20", title="b")

    assert loser.has_metadata_absent_from(keeper) == {}


def test_loser_scalar_filling_a_gap_is_reported():
    keeper = make_scene("10")
    loser = make_scene("20", director="D")

    assert loser.has_metadata_absent_from(keeper) == {"director": "D"}
