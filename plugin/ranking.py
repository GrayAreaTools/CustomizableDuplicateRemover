"""Candidate ranking.

Each rank key maps a candidate to a value where **smaller is better**, so a group's
keeper is `sorted(candidates, key=rank_tuple)[0]`. `file_id` is appended to every rank
order, making the ordering total and every run reproducible regardless of the order
Stash returned the group in.
"""

from __future__ import annotations

import hashlib
from typing import Callable, Optional

from config_schema import Config, normalise_codec
from models import Candidate

# A rank key returns a float or int; smaller wins.
RankFn = Callable[[Candidate, Config], float]


def _codec(candidate: Candidate, config: Config) -> float:
    """Position in `codecPreference`. Unlisted codecs sort last."""
    codec = normalise_codec(candidate.file.video_codec)
    try:
        return config.codec_preference.index(codec)
    except ValueError:
        return len(config.codec_preference)


def _audio_codec(candidate: Candidate, config: Config) -> float:
    codec = normalise_codec(candidate.file.audio_codec)
    try:
        return config.audio_codec_preference.index(codec)
    except ValueError:
        return len(config.audio_codec_preference)


def _path_priority(candidate: Candidate, config: Config) -> float:
    """Position of the first `preferredPaths` prefix the file's path starts with."""
    for index, prefix in enumerate(config.preferred_paths):
        if candidate.file.path.startswith(prefix):
            return index
    return len(config.preferred_paths)


def _age(candidate: Candidate, _: Config) -> float:
    """Older is better — but an unknown mod time must never win.

    `candidates.parse_mod_time` degrades an absent or unparseable timestamp to 0.0, which
    as a "smaller is better" key made a file with broken metadata the preferred keeper of
    its whole group. Treat unknown as newest instead, so it loses.
    """
    return candidate.file.mod_time if candidate.file.mod_time > 0 else float("inf")


def _age_desc(candidate: Candidate, _: Config) -> float:
    """Newer is better; unknown loses here too."""
    return -candidate.file.mod_time if candidate.file.mod_time > 0 else float("inf")


RANK_FUNCTIONS: dict[str, RankFn] = {
    "codec": _codec,
    "audio_codec": _audio_codec,
    "path_priority": _path_priority,
    "resolution": lambda c, _: -c.file.pixels,
    "resolution_asc": lambda c, _: c.file.pixels,
    "bitrate": lambda c, _: -c.file.bit_rate,
    "bitrate_asc": lambda c, _: c.file.bit_rate,
    "size": lambda c, _: -c.file.size,
    "size_asc": lambda c, _: c.file.size,
    "framerate": lambda c, _: -c.file.frame_rate,
    "duration": lambda c, _: -c.file.duration,
    "age": _age,
    "age_desc": _age_desc,
    "organized": lambda c, _: 0 if c.scene.organized else 1,
    "file_id": lambda c, _: _numeric_id(c.file.id),
}


def _numeric_id(value: str) -> float:
    """Stash ids are numeric strings; fall back to a digest if that ever changes.

    Not `hash()`: that is salted by PYTHONHASHSEED, so it differs between processes and
    would have broken the reproducibility this key exists to provide. `float(large int)`
    also loses precision, which could make two ids collide and leave the ordering
    non-total, so the digest is truncated to a range floats represent exactly.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]
        return float(int(digest, 16))


def effective_rank_order(config: Config) -> tuple[str, ...]:
    """The configured order with `file_id` guaranteed last."""
    order = tuple(key for key in config.rank_order if key != "file_id")
    return order + ("file_id",)


def rank_tuple(candidate: Candidate, config: Config) -> tuple[float, ...]:
    return tuple(
        RANK_FUNCTIONS[key](candidate, config) for key in effective_rank_order(config)
    )


def policy_tuple(candidate: Candidate, config: Config) -> tuple[float, ...]:
    """The rank tuple without the terminal `file_id` key.

    Two candidates with equal policy tuples are a genuine tie: the operator's policy
    does not distinguish them. `file_id` would break the tie arbitrarily, so it is
    excluded here and the tie breaker gets to decide instead.
    """
    order = [key for key in effective_rank_order(config) if key != "file_id"]
    return tuple(RANK_FUNCTIONS[key](candidate, config) for key in order)


def deciding_key(winner: Candidate, runner_up: Candidate, config: Config) -> str:
    """The first rank key on which `winner` beat `runner_up`.

    Recorded in the plan so a surprising keeper choice is traceable to the key
    responsible, rather than leaving the operator to reverse-engineer it.
    """
    for key in effective_rank_order(config):
        fn = RANK_FUNCTIONS[key]
        win, lose = fn(winner, config), fn(runner_up, config)
        if win != lose:
            return key
    return "none"


# Tie breakers. Each takes the tied candidates and returns the one to keep.

def _smallest(tied: list[Candidate]) -> Candidate:
    return min(tied, key=lambda c: (c.file.size, _numeric_id(c.file.id)))


def _largest(tied: list[Candidate]) -> Candidate:
    return min(tied, key=lambda c: (-c.file.size, _numeric_id(c.file.id)))


def _oldest(tied: list[Candidate]) -> Candidate:
    return min(tied, key=lambda c: (_age(c, None), _numeric_id(c.file.id)))


def _newest(tied: list[Candidate]) -> Candidate:
    return min(tied, key=lambda c: (_age_desc(c, None), _numeric_id(c.file.id)))


def _highest_resolution_subset(tied: list[Candidate]) -> list[Candidate]:
    best = max(c.file.pixels for c in tied)
    return [c for c in tied if c.file.pixels == best]


TIE_BREAKERS: dict[str, Callable[[list[Candidate]], Candidate]] = {
    "smallest": _smallest,
    "largest": _largest,
    "oldest": _oldest,
    "newest": _newest,
    "smallest_of_highest_resolution": lambda tied: _smallest(_highest_resolution_subset(tied)),
    "largest_of_highest_resolution": lambda tied: _largest(_highest_resolution_subset(tied)),
}


class Ranking:
    """The result of ranking one group."""

    def __init__(
        self,
        ordered: list[Candidate],
        keeper: Optional[Candidate],
        tied: list[Candidate],
        deciding: str,
    ):
        self.ordered = ordered
        self.keeper = keeper
        self.tied = tied
        self.deciding = deciding

    @property
    def is_ambiguous(self) -> bool:
        return self.keeper is None


def rank_group(candidates: list[Candidate], config: Config) -> Ranking:
    """Order a group's candidates and pick the keeper.

    Returns a `Ranking` whose `keeper` is None only when the policy ties and
    `tieBreaker` is `skip` — the group is then reported AMBIGUOUS rather than resolved
    by an arbitrary choice.
    """
    if not candidates:
        return Ranking([], None, [], "none")

    ordered = sorted(candidates, key=lambda c: rank_tuple(c, config))
    if len(ordered) == 1:
        return Ranking(ordered, ordered[0], [], "none")

    best_policy = policy_tuple(ordered[0], config)
    tied = [c for c in ordered if policy_tuple(c, config) == best_policy]

    if len(tied) == 1:
        return Ranking(ordered, ordered[0], [], deciding_key(ordered[0], ordered[1], config))

    if config.tie_breaker == "skip":
        return Ranking(ordered, None, tied, "tie")

    keeper = TIE_BREAKERS[config.tie_breaker](tied)
    return Ranking(ordered, keeper, tied, f"tieBreaker:{config.tie_breaker}")


def ranks_strictly_better_codec(keeper: Candidate, loser: Candidate, config: Config) -> bool:
    """Whether the keeper's codec is preferred over the loser's.

    `G_QUALITY_FLOOR` uses this to allow a lower bitrate ratio when the keeper is a
    more efficient codec.
    """
    return _codec(keeper, config) < _codec(loser, config)
