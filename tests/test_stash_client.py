"""Client behaviour: batching, connection handling, and mutation shapes."""

import pytest

from stash_client import BULK_TIMEOUT, TAG_BATCH_SIZE, StashClient, StashError


class RecordingClient(StashClient):
    """Records calls instead of performing them."""

    def __init__(self, connection=None, *, responses=None, fail_after=None):
        super().__init__(connection or {})
        self.calls = []
        self.responses = responses or {}
        self.fail_after = fail_after

    def call(self, query, variables=None, *, timeout=None, operation="query"):
        self.calls.append(
            {"query": query, "variables": variables or {}, "timeout": timeout,
             "operation": operation}
        )
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            raise StashError("STASH_UNREACHABLE", "timed out")
        for fragment, response in self.responses.items():
            if fragment in query:
                return response
        return {"bulkSceneUpdate": [{"id": "1"}]}


# -- connection handling ----------------------------------------------------

def test_bind_address_is_rewritten_to_loopback():
    """Stash reports its bind address, which is not necessarily routable."""
    assert StashClient({"Host": "0.0.0.0", "Port": 9999}).url == "http://127.0.0.1:9999/graphql"
    assert StashClient({"Host": "::", "Port": 9999}).url == "http://127.0.0.1:9999/graphql"


def test_connection_keys_are_case_insensitive():
    client = StashClient({"scheme": "https", "host": "stash.local", "port": 443})

    assert client.url == "https://stash.local:443/graphql"


def test_session_cookie_becomes_a_cookie_header():
    client = StashClient({"SessionCookie": {"Name": "session", "Value": "abc123"}})

    assert client.headers["Cookie"] == "session=abc123"


def test_api_key_becomes_a_header():
    client = StashClient({"ApiKey": "key-value"})

    assert client.headers["ApiKey"] == "key-value"


def test_missing_credentials_leave_no_auth_headers():
    client = StashClient({})

    assert "Cookie" not in client.headers
    assert "ApiKey" not in client.headers


def test_defaults_apply_when_connection_is_empty():
    assert StashClient({}).url == "http://localhost:9999/graphql"
    assert StashClient(None).url == "http://localhost:9999/graphql"


# -- tag batching -----------------------------------------------------------

def test_bulk_tag_batches_large_scene_lists():
    """Regression: one mutation for hundreds of scenes exceeded the request timeout,
    because every scene update runs other plugins' Scene.Update.Post hooks inline."""
    client = RecordingClient()
    scene_ids = [str(i) for i in range(1, 331)]

    client.add_tag(scene_ids, "tag1")

    assert len(client.calls) == 7  # ceil(330 / 50)
    sizes = [len(call["variables"]["ids"]) for call in client.calls]
    assert sizes == [50, 50, 50, 50, 50, 50, 30]
    assert sum(sizes) == 330


def test_bulk_tag_covers_every_scene_exactly_once():
    client = RecordingClient()
    scene_ids = [str(i) for i in range(1, 121)]

    client.add_tag(scene_ids, "tag1")

    tagged = [i for call in client.calls for i in call["variables"]["ids"]]
    assert sorted(tagged, key=int) == sorted(scene_ids, key=int)
    assert len(tagged) == len(set(tagged))


def test_bulk_tag_deduplicates_input():
    client = RecordingClient()

    client.add_tag(["5", "5", "5", "7"], "tag1")

    assert len(client.calls) == 1
    assert client.calls[0]["variables"]["ids"] == ["5", "7"]


def test_bulk_tag_uses_the_longer_timeout():
    client = RecordingClient()

    client.add_tag(["1"], "tag1")

    assert client.calls[0]["timeout"] == BULK_TIMEOUT
    assert BULK_TIMEOUT > 30.0


def test_bulk_tag_batch_size_is_bounded():
    assert 0 < TAG_BATCH_SIZE <= 100


def test_bulk_tag_on_empty_list_makes_no_calls():
    client = RecordingClient()

    assert client.add_tag([], "tag1") is True
    assert client.calls == []


def test_add_and_remove_use_the_documented_modes():
    client = RecordingClient()

    client.add_tag(["1"], "tag1")
    client.remove_tag(["1"], "tag1")

    assert client.calls[0]["variables"]["mode"] == "ADD"
    assert client.calls[1]["variables"]["mode"] == "REMOVE"


def test_bulk_tag_raises_when_a_batch_returns_nothing():
    """A null response means the update did not apply; that must not pass silently."""
    client = RecordingClient(responses={"bulkSceneUpdate": {"bulkSceneUpdate": None}})

    with pytest.raises(StashError) as excinfo:
        client.add_tag(["1", "2"], "tag1")

    assert excinfo.value.code == "BULK_TAG_FAILED"


def test_bulk_tag_propagates_a_failure_partway_through():
    """A later batch failing must surface, not be swallowed after earlier successes."""
    client = RecordingClient(fail_after=2)

    with pytest.raises(StashError) as excinfo:
        client.add_tag([str(i) for i in range(1, 200)], "tag1")

    assert excinfo.value.code == "STASH_UNREACHABLE"
    assert len(client.calls) == 3


# -- mutation shapes --------------------------------------------------------

def test_destroy_scenes_passes_both_flags_explicitly():
    client = RecordingClient(responses={"scenesDestroy": {"scenesDestroy": True}})

    client.destroy_scenes(["1"], delete_file=True, delete_generated=False)

    variables = client.calls[0]["variables"]
    assert variables["delete_file"] is True
    assert variables["delete_generated"] is False


def test_delete_files_on_empty_list_makes_no_call():
    client = RecordingClient()

    assert client.delete_files([]) is True
    assert client.calls == []


def test_destroy_scenes_on_empty_list_makes_no_call():
    client = RecordingClient()

    assert client.destroy_scenes([], delete_file=True, delete_generated=True) is True
    assert client.calls == []


def test_merge_scenes_uses_the_longer_timeout():
    client = RecordingClient(responses={"sceneMerge": {"sceneMerge": {"id": "10"}}})

    client.merge_scenes(["20"], "10")

    assert client.calls[0]["timeout"] == 120.0


def test_find_or_create_tag_reuses_an_existing_tag():
    client = RecordingClient(
        responses={"findTags": {"findTags": {"tags": [{"id": "7", "name": "CDR: Keep"}]}}}
    )

    assert client.find_or_create_tag("CDR: Keep") == "7"
    assert len(client.calls) == 1  # no tagCreate


def test_find_or_create_tag_creates_when_absent():
    client = RecordingClient(
        responses={
            "findTags": {"findTags": {"tags": []}},
            "tagCreate": {"tagCreate": {"id": "9"}},
        }
    )

    assert client.find_or_create_tag("CDR: Keep") == "9"
    assert [c["operation"] for c in client.calls] == ["findTags", "tagCreate"]


def test_find_or_create_tag_raises_if_creation_returns_no_id():
    client = RecordingClient(
        responses={
            "findTags": {"findTags": {"tags": []}},
            "tagCreate": {"tagCreate": {}},
        }
    )

    with pytest.raises(StashError) as excinfo:
        client.find_or_create_tag("CDR: Keep")

    assert excinfo.value.code == "TAG_CREATE_FAILED"
