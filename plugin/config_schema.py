"""Settings parsing and validation.

Stash plugin settings are limited to STRING, NUMBER, and BOOLEAN, so lists and enums
arrive as comma-separated strings. Every value is validated here and an invalid one
aborts the run naming the setting, the offending token, and the accepted values. There
is no silent fallback to a default — a typo in `rankOrder` must not quietly delete the
wrong files.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from models import ParseError

# Rank keys, in the direction documented in docs/spec.md §5.1. `file_id` is appended
# implicitly by ranking.py and so is accepted here but never required.
VALID_RANK_KEYS = (
    "codec",
    "resolution",
    "resolution_asc",
    "bitrate",
    "bitrate_asc",
    "size",
    "size_asc",
    "framerate",
    "duration",
    "age",
    "age_desc",
    "audio_codec",
    "path_priority",
    "organized",
    "file_id",
)

VALID_TIE_BREAKERS = (
    "skip",
    "smallest_of_highest_resolution",
    "largest_of_highest_resolution",
    "smallest",
    "largest",
    "oldest",
    "newest",
)

VALID_METADATA_POLICIES = ("merge", "skip", "ignore")

DEFAULTS: dict[str, Any] = {
    "rankOrder": "codec,resolution,bitrate,size,age",
    "codecPreference": "av1,hevc,h264,vp9,mpeg4,vc1,wmv3,msmpeg4v3,mpeg2video",
    "audioCodecPreference": "opus,aac,ac3,mp3",
    "tieBreaker": "skip",
    "phashDistance": 0,
    "durationDiff": 1.0,
    "durationTolerance": 1.0,
    "qualityFloorRatio": 0.35,
    "qualityFloorRatioCrossCodec": 0.15,
    "protectedPaths": "",
    "preferredPaths": "",
    "metadataPolicy": "merge",
    "maxDeletionsPerRun": 100,
    "maxFractionOfLibrary": 0.05,
    "applyTags": False,
    "confirmDestructive": False,
    "planRetention": 5,
    "reportDir": "",
}

# Operator input is forgiving about codec naming; Stash reports ffmpeg's names, where
# HEVC is "hevc" and never "h265".
CODEC_ALIASES = {
    "h265": "hevc",
    "x265": "hevc",
    "h.265": "hevc",
    "hvc1": "hevc",
    "hev1": "hevc",
    "avc": "h264",
    "avc1": "h264",
    "x264": "h264",
    "h.264": "h264",
    "divx": "mpeg4",
    "xvid": "mpeg4",
    "av01": "av1",
}

# Suggested codecs for the settings picker, most to least efficient. Not a whitelist:
# `codecPreference` accepts any name ffmpeg might report, and an unlisted codec simply
# sorts last.
KNOWN_VIDEO_CODECS = (
    "av1", "hevc", "h264", "vp9", "vp8", "mpeg4", "vc1", "wmv3", "msmpeg4v3",
    "mpeg2video", "mpeg1video", "theora", "flv1", "svq3", "cinepak", "rv40",
)

KNOWN_AUDIO_CODECS = (
    "opus", "aac", "ac3", "eac3", "dts", "flac", "vorbis", "mp3", "mp2", "pcm_s16le",
    "wmav2",
)

MAX_SETTING_LENGTH = 4096
MAX_LIST_ITEMS = 64


def normalise_codec(value: Optional[str]) -> str:
    """Lowercase and de-alias a codec name. Unknown input passes through normalised."""
    if not value:
        return ""
    cleaned = value.strip().lower()
    return CODEC_ALIASES.get(cleaned, cleaned)


def _bounded(setting: str, raw: Any) -> str:
    text = "" if raw is None else str(raw)
    if len(text) > MAX_SETTING_LENGTH:
        raise ParseError(
            "SETTING_TOO_LONG",
            f"Setting '{setting}' exceeds {MAX_SETTING_LENGTH} characters.",
            {"setting": setting, "length": len(text)},
        )
    return text


def _parse_list(setting: str, raw: Any, *, lower: bool = True) -> tuple[str, ...]:
    text = _bounded(setting, raw)
    items = [part.strip() for part in text.split(",")]
    items = [part.lower() if lower else part for part in items if part]
    if len(items) > MAX_LIST_ITEMS:
        raise ParseError(
            "SETTING_LIST_TOO_LONG",
            f"Setting '{setting}' has {len(items)} entries; the maximum is {MAX_LIST_ITEMS}.",
            {"setting": setting, "count": len(items)},
        )
    seen: set[str] = set()
    for item in items:
        if item in seen:
            raise ParseError(
                "SETTING_DUPLICATE_ENTRY",
                f"Setting '{setting}' lists '{item}' more than once.",
                {"setting": setting, "token": item},
            )
        seen.add(item)
    return tuple(items)


def _parse_enum(setting: str, raw: Any, allowed: tuple[str, ...], default: str) -> str:
    text = _bounded(setting, raw).strip().lower()
    if not text:
        return default
    if text not in allowed:
        raise ParseError(
            "SETTING_INVALID_ENUM",
            f"Setting '{setting}' is '{text}'; accepted values are: {', '.join(allowed)}.",
            {"setting": setting, "token": text, "allowed": list(allowed)},
        )
    return text


def _parse_number(
    setting: str,
    raw: Any,
    default: float,
    *,
    minimum: float,
    maximum: float,
    integral: bool = False,
) -> float:
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ParseError(
            "SETTING_NOT_A_NUMBER",
            f"Setting '{setting}' is '{raw}', which is not a number.",
            {"setting": setting, "token": str(raw)},
        ) from None
    if value != value or value in (float("inf"), float("-inf")):
        raise ParseError(
            "SETTING_NOT_FINITE",
            f"Setting '{setting}' must be a finite number.",
            {"setting": setting, "token": str(raw)},
        )
    if not minimum <= value <= maximum:
        raise ParseError(
            "SETTING_OUT_OF_RANGE",
            f"Setting '{setting}' is {value}; it must be between {minimum} and {maximum}.",
            {"setting": setting, "value": value, "min": minimum, "max": maximum},
        )
    if integral:
        if value != int(value):
            raise ParseError(
                "SETTING_NOT_INTEGRAL",
                f"Setting '{setting}' is {value}; it must be a whole number.",
                {"setting": setting, "value": value},
            )
        return int(value)
    return value


def _parse_bool(setting: str, raw: Any, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    raise ParseError(
        "SETTING_NOT_A_BOOLEAN",
        f"Setting '{setting}' is '{raw}'; expected true or false.",
        {"setting": setting, "token": str(raw)},
    )


def _parse_paths(setting: str, raw: Any) -> tuple[str, ...]:
    """Path prefixes keep their case — filesystems may be case-sensitive."""
    text = _bounded(setting, raw)
    items = tuple(part.strip() for part in text.split(",") if part.strip())
    if len(items) > MAX_LIST_ITEMS:
        raise ParseError(
            "SETTING_LIST_TOO_LONG",
            f"Setting '{setting}' has {len(items)} entries; the maximum is {MAX_LIST_ITEMS}.",
            {"setting": setting, "count": len(items)},
        )
    return items


@dataclass(frozen=True)
class Config:
    rank_order: tuple[str, ...]
    codec_preference: tuple[str, ...]
    audio_codec_preference: tuple[str, ...]
    tie_breaker: str
    phash_distance: int
    duration_diff: float
    duration_tolerance: float
    quality_floor_ratio: float
    quality_floor_ratio_cross_codec: float
    protected_paths: tuple[str, ...]
    preferred_paths: tuple[str, ...]
    metadata_policy: str
    max_deletions_per_run: int
    max_fraction_of_library: float
    apply_tags: bool
    confirm_destructive: bool
    plan_retention: int
    report_dir: str

    def as_dict(self) -> dict:
        """The effective configuration, recorded in every plan and run summary."""
        return {
            "rankOrder": list(self.rank_order),
            "codecPreference": list(self.codec_preference),
            "audioCodecPreference": list(self.audio_codec_preference),
            "tieBreaker": self.tie_breaker,
            "phashDistance": self.phash_distance,
            "durationDiff": self.duration_diff,
            "durationTolerance": self.duration_tolerance,
            "qualityFloorRatio": self.quality_floor_ratio,
            "qualityFloorRatioCrossCodec": self.quality_floor_ratio_cross_codec,
            "protectedPaths": list(self.protected_paths),
            "preferredPaths": list(self.preferred_paths),
            "metadataPolicy": self.metadata_policy,
            "maxDeletionsPerRun": self.max_deletions_per_run,
            "maxFractionOfLibrary": self.max_fraction_of_library,
            "applyTags": self.apply_tags,
            "confirmDestructive": self.confirm_destructive,
            "planRetention": self.plan_retention,
            "reportDir": self.report_dir,
        }


def parse_config(raw: Optional[dict], *, default_report_dir: str = "") -> Config:
    """Validate raw plugin settings into a frozen `Config`.

    Raises `ParseError` on any invalid value. Missing values take the documented
    default; present-but-invalid values never do.
    """
    settings = dict(raw or {})

    rank_order = _parse_list("rankOrder", settings.get("rankOrder") or DEFAULTS["rankOrder"])
    if not rank_order:
        raise ParseError(
            "SETTING_EMPTY", "Setting 'rankOrder' must list at least one rank key.",
            {"setting": "rankOrder", "allowed": list(VALID_RANK_KEYS)},
        )
    for token in rank_order:
        if token not in VALID_RANK_KEYS:
            raise ParseError(
                "SETTING_UNKNOWN_RANK_KEY",
                f"Setting 'rankOrder' contains unknown key '{token}'. "
                f"Valid keys are: {', '.join(VALID_RANK_KEYS)}.",
                {"setting": "rankOrder", "token": token, "allowed": list(VALID_RANK_KEYS)},
            )

    codec_preference = tuple(
        normalise_codec(item)
        for item in _parse_list(
            "codecPreference", settings.get("codecPreference") or DEFAULTS["codecPreference"]
        )
    )
    if not codec_preference:
        raise ParseError(
            "SETTING_EMPTY",
            "Setting 'codecPreference' must list at least one codec.",
            {"setting": "codecPreference"},
        )
    # Aliasing can collapse two distinct tokens onto one codec, which would make the
    # ranking depend on input order in a way the operator did not intend.
    if len(set(codec_preference)) != len(codec_preference):
        raise ParseError(
            "SETTING_DUPLICATE_ENTRY",
            "Setting 'codecPreference' lists the same codec twice after alias "
            "normalisation (for example both 'hevc' and 'h265').",
            {"setting": "codecPreference", "normalised": list(codec_preference)},
        )

    audio_codec_preference = tuple(
        normalise_codec(item)
        for item in _parse_list(
            "audioCodecPreference",
            settings.get("audioCodecPreference") or DEFAULTS["audioCodecPreference"],
        )
    )

    preferred_paths = _parse_paths("preferredPaths", settings.get("preferredPaths"))
    if "path_priority" in rank_order and not preferred_paths:
        raise ParseError(
            "SETTING_DEPENDENCY",
            "Setting 'rankOrder' uses 'path_priority' but 'preferredPaths' is empty.",
            {"setting": "preferredPaths"},
        )

    report_dir = _bounded("reportDir", settings.get("reportDir")).strip() or default_report_dir

    return Config(
        rank_order=rank_order,
        codec_preference=codec_preference,
        audio_codec_preference=audio_codec_preference,
        tie_breaker=_parse_enum(
            "tieBreaker", settings.get("tieBreaker"), VALID_TIE_BREAKERS, DEFAULTS["tieBreaker"]
        ),
        phash_distance=int(
            _parse_number(
                "phashDistance", settings.get("phashDistance"), DEFAULTS["phashDistance"],
                minimum=0, maximum=64, integral=True,
            )
        ),
        duration_diff=_parse_number(
            "durationDiff", settings.get("durationDiff"), DEFAULTS["durationDiff"],
            minimum=0.0, maximum=3600.0,
        ),
        duration_tolerance=_parse_number(
            "durationTolerance", settings.get("durationTolerance"),
            DEFAULTS["durationTolerance"], minimum=0.0, maximum=3600.0,
        ),
        quality_floor_ratio=_parse_number(
            "qualityFloorRatio", settings.get("qualityFloorRatio"),
            DEFAULTS["qualityFloorRatio"], minimum=0.0, maximum=10.0,
        ),
        quality_floor_ratio_cross_codec=_parse_number(
            "qualityFloorRatioCrossCodec", settings.get("qualityFloorRatioCrossCodec"),
            DEFAULTS["qualityFloorRatioCrossCodec"], minimum=0.0, maximum=10.0,
        ),
        protected_paths=_parse_paths("protectedPaths", settings.get("protectedPaths")),
        preferred_paths=preferred_paths,
        metadata_policy=_parse_enum(
            "metadataPolicy", settings.get("metadataPolicy"),
            VALID_METADATA_POLICIES, DEFAULTS["metadataPolicy"],
        ),
        max_deletions_per_run=int(
            _parse_number(
                "maxDeletionsPerRun", settings.get("maxDeletionsPerRun"),
                DEFAULTS["maxDeletionsPerRun"], minimum=0, maximum=100000, integral=True,
            )
        ),
        max_fraction_of_library=_parse_number(
            "maxFractionOfLibrary", settings.get("maxFractionOfLibrary"),
            DEFAULTS["maxFractionOfLibrary"], minimum=0.0, maximum=1.0,
        ),
        apply_tags=_parse_bool("applyTags", settings.get("applyTags"), DEFAULTS["applyTags"]),
        confirm_destructive=_parse_bool(
            "confirmDestructive", settings.get("confirmDestructive"),
            DEFAULTS["confirmDestructive"],
        ),
        plan_retention=int(
            _parse_number(
                "planRetention", settings.get("planRetention"), DEFAULTS["planRetention"],
                minimum=1, maximum=1000, integral=True,
            )
        ),
        report_dir=report_dir,
    )
