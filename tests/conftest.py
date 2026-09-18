import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))

from config_schema import parse_config  # noqa: E402
from models import Candidate, FileInfo, SceneInfo  # noqa: E402


def make_file(
    file_id="1",
    *,
    path=None,
    codec="h264",
    width=1920,
    height=1080,
    size=1_000_000_000,
    bit_rate=8_000_000,
    duration=3600.0,
    frame_rate=30.0,
    mod_time=1_700_000_000.0,
    audio_codec="aac",
) -> FileInfo:
    return FileInfo(
        id=str(file_id),
        path=path or f"/media/library/file{file_id}.mkv",
        basename=f"file{file_id}.mkv",
        size=size,
        mod_time=mod_time,
        width=width,
        height=height,
        duration=duration,
        video_codec=codec,
        audio_codec=audio_codec,
        frame_rate=frame_rate,
        bit_rate=bit_rate,
        phash="abc123",
    )


def make_scene(
    scene_id="10",
    *,
    file_ids=None,
    organized=False,
    o_counter=0,
    rating100=None,
    tag_ids=(),
    performer_ids=(),
    urls=(),
    marker_count=0,
    play_count=0,
    primary_file_id=None,
) -> SceneInfo:
    ids = tuple(str(f) for f in (file_ids or ["1"]))
    return SceneInfo(
        id=str(scene_id),
        title=f"scene {scene_id}",
        organized=organized,
        o_counter=o_counter,
        rating100=rating100,
        tag_ids=frozenset(str(t) for t in tag_ids),
        performer_ids=frozenset(str(p) for p in performer_ids),
        urls=frozenset(urls),
        marker_count=marker_count,
        play_count=play_count,
        file_ids=ids,
        primary_file_id=primary_file_id or ids[0],
    )


def make_candidate(file_id="1", scene_id="10", **file_kwargs) -> Candidate:
    file = make_file(file_id, **file_kwargs)
    scene = make_scene(scene_id, file_ids=[file.id])
    return Candidate(scene=scene, file=file)


class FakeFileSystem:
    """In-memory filesystem for guard rail tests."""

    def __init__(self, files=None, links=None, unreadable=()):
        self.files = dict(files or {})  # path -> size
        self.links = dict(links or {})  # path -> real path
        self.unreadable = set(unreadable)

    def exists(self, path):
        return self.realpath(path) in self.files

    def size(self, path):
        real = self.realpath(path)
        if real not in self.files:
            raise OSError(f"no such file: {path}")
        return self.files[real]

    def readable(self, path):
        return path not in self.unreadable and self.realpath(path) not in self.unreadable

    def realpath(self, path):
        return self.links.get(path, path)


@pytest.fixture
def config():
    """Default configuration: codec-first ranking, skip on ties."""
    return parse_config({})


@pytest.fixture
def fs():
    return FakeFileSystem()


@pytest.fixture
def rng():
    return random.Random(20260731)
