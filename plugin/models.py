"""Immutable data model for duplicate resolution.

Everything downstream of `candidates.build_groups` works on these types rather than
raw GraphQL dicts, so ranking, guard rails, and planning stay pure and testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ParseError(ValueError):
    """Raised when input from Stash or from settings cannot be interpreted."""

    def __init__(self, code: str, message: str, context: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = context or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "context": self.context}


@dataclass(frozen=True)
class FileInfo:
    """One `VideoFile` from Stash.

    `size` arrives as the GraphQL `Int64` scalar, which some clients serialise as a
    string; `candidates.py` coerces it explicitly rather than trusting the type.
    """

    id: str
    path: str
    basename: str
    size: int
    mod_time: float  # epoch seconds
    width: int
    height: int
    duration: float
    video_codec: str
    audio_codec: str
    frame_rate: float
    bit_rate: int
    phash: Optional[str] = None

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def bits_per_pixel_frame(self) -> float:
        """Bitrate normalised for resolution and frame rate.

        The comparable quality measure across differing resolutions. Returns 0.0 when
        any input is missing so `G_QUALITY_FLOOR` can treat it as unknown rather than
        dividing by zero.
        """
        divisor = self.pixels * self.frame_rate
        if divisor <= 0 or self.bit_rate <= 0:
            return 0.0
        return self.bit_rate / divisor


@dataclass(frozen=True)
class SceneInfo:
    """The scene a file belongs to, with the metadata `G_METADATA` inspects."""

    id: str
    title: str
    organized: bool
    o_counter: int
    rating100: Optional[int]
    tag_ids: frozenset[str]
    performer_ids: frozenset[str]
    urls: frozenset[str]
    marker_count: int
    play_count: int
    file_ids: tuple[str, ...]
    primary_file_id: Optional[str]

    @property
    def is_multi_file(self) -> bool:
        return len(self.file_ids) > 1

    def has_metadata_absent_from(self, other: "SceneInfo") -> dict:
        """Metadata this scene carries that `other` lacks.

        Used by `G_METADATA` to decide whether destroying this scene would lose
        curation work. Returns an empty dict when nothing would be lost.
        """
        lost: dict = {}
        if self.tag_ids - other.tag_ids:
            lost["tag_ids"] = sorted(self.tag_ids - other.tag_ids)
        if self.performer_ids - other.performer_ids:
            lost["performer_ids"] = sorted(self.performer_ids - other.performer_ids)
        if self.urls - other.urls:
            lost["urls"] = sorted(self.urls - other.urls)
        if self.rating100 is not None and other.rating100 is None:
            lost["rating100"] = self.rating100
        if self.o_counter > other.o_counter:
            lost["o_counter"] = self.o_counter
        if self.play_count > other.play_count:
            lost["play_count"] = self.play_count
        if self.marker_count > 0:
            lost["marker_count"] = self.marker_count
        return lost


def merge_values(keeper: "SceneInfo", sources: "list[SceneInfo]") -> dict:
    """A `SceneUpdateInput` that unions the sources' metadata into the keeper.

    `sceneMerge` applies `values` verbatim over the destination and copies nothing
    itself, so the union has to be computed here or the sources' curation is lost.
    Scalars are only supplied when the keeper has none — a merge must never overwrite
    something the operator set on the file they chose to keep.
    """
    values: dict = {"id": keeper.id}

    tag_ids = set(keeper.tag_ids)
    performer_ids = set(keeper.performer_ids)
    urls = set(keeper.urls)
    for source in sources:
        tag_ids |= source.tag_ids
        performer_ids |= source.performer_ids
        urls |= source.urls

    if tag_ids != keeper.tag_ids:
        values["tag_ids"] = sorted(tag_ids)
    if performer_ids != keeper.performer_ids:
        values["performer_ids"] = sorted(performer_ids)
    if urls != keeper.urls:
        values["urls"] = sorted(urls)

    if keeper.rating100 is None:
        ratings = [s.rating100 for s in sources if s.rating100 is not None]
        if ratings:
            values["rating100"] = max(ratings)

    return values


@dataclass(frozen=True)
class Candidate:
    """A `(scene, file)` pair — the unit ranking operates on.

    A group of 3 scenes where one scene holds 2 files yields 4 candidates. Ranking
    scenes alone (what `DupFileManager` does via `files[0]`) cannot express this.
    """

    scene: SceneInfo
    file: FileInfo

    @property
    def key(self) -> str:
        return f"{self.scene.id}:{self.file.id}"

    @property
    def is_primary_file(self) -> bool:
        return self.scene.primary_file_id == self.file.id


class GroupStatus(str, Enum):
    RESOLVED = "RESOLVED"
    # Applied. Distinct from RESOLVED so a plan that has already run cannot be mistaken
    # for one still awaiting review.
    EXECUTED = "EXECUTED"
    SKIPPED = "SKIPPED"
    AMBIGUOUS = "AMBIGUOUS"
    PROTECTED = "PROTECTED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


class Action(str, Enum):
    DELETE_FILE = "DELETE_FILE"
    DESTROY_SCENE = "DESTROY_SCENE"
    REASSIGN_PRIMARY = "REASSIGN_PRIMARY"
    MERGE_METADATA = "MERGE_METADATA"


@dataclass(frozen=True)
class Verdict:
    """A guard rail's decision. `allowed=False` blocks the operation."""

    rail: str
    allowed: bool
    reason: str = ""
    context: dict = field(default_factory=dict)


@dataclass
class CandidatePlan:
    """What the plan intends to do with one candidate, and what became of it."""

    candidate: Candidate
    is_keeper: bool
    action: Optional[Action] = None
    verdicts: list[Verdict] = field(default_factory=list)
    # Set once the executor has acted: "deleted", "blocked", or "failed". Its presence
    # means this candidate is history, not pending work.
    outcome: Optional[str] = None

    @property
    def is_pending(self) -> bool:
        return self.outcome is None

    @property
    def blocked_by(self) -> list[Verdict]:
        return [v for v in self.verdicts if not v.allowed]

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocked_by)


@dataclass
class GroupPlan:
    """The resolution of one phash duplicate group."""

    index: int
    status: GroupStatus
    candidates: list[CandidatePlan]
    deciding_key: str = ""
    reason: str = ""
    fingerprint: str = ""

    @property
    def keeper(self) -> Optional[CandidatePlan]:
        for entry in self.candidates:
            if entry.is_keeper:
                return entry
        return None

    @property
    def losers(self) -> list[CandidatePlan]:
        """Candidates this plan will act on.

        Excludes keepers, blocked entries, and anything already acted on — so an
        executed plan reports no outstanding work and cannot be re-applied to files
        that are already gone.
        """
        return [
            entry
            for entry in self.candidates
            if not entry.is_keeper
            and not entry.is_blocked
            and entry.action
            and entry.is_pending
        ]

    @property
    def acted_on(self) -> list[CandidatePlan]:
        return [entry for entry in self.candidates if entry.outcome]

    @property
    def bytes_reclaimed(self) -> int:
        return sum(entry.candidate.file.size for entry in self.losers)
