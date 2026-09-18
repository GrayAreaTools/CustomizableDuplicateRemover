"""Minimal Stash GraphQL client.

Deliberately built on `urllib` rather than `stashapp-tools`: the plugin runs inside the
Stash container where the operator cannot be assumed to have installed extra packages,
and the calls needed here are few. `stashapp-tools` also hardcodes
`delete_generated: true` on scene destruction, which `G_GENERATED` needs to control.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from candidates import SCENE_FRAGMENT

DEFAULT_TIMEOUT = 30.0
MERGE_TIMEOUT = 120.0
# Bulk scene updates trigger every other plugin's Scene.Update.Post hooks synchronously,
# so they need both a longer timeout and a bounded batch size.
BULK_TIMEOUT = 120.0
TAG_BATCH_SIZE = 50
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 1.5
MAX_RESPONSE_BYTES = 512 * 1024 * 1024


class StashError(RuntimeError):
    """A GraphQL or transport failure, with enough context to reproduce."""

    def __init__(self, code: str, message: str, context: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.context = context or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "context": self.context}


class StashClient:
    def __init__(self, server_connection: dict, logger=None):
        connection = {str(k).lower(): v for k, v in (server_connection or {}).items()}
        scheme = connection.get("scheme") or "http"
        host = connection.get("host") or "localhost"
        # Stash reports its bind address, which is not necessarily routable.
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        port = connection.get("port") or 9999
        self.url = f"{scheme}://{host}:{port}/graphql"

        self.headers = {"Content-Type": "application/json", "Accept": "application/json"}
        cookie = connection.get("sessioncookie") or {}
        if isinstance(cookie, dict) and cookie.get("Value"):
            name = cookie.get("Name") or "session"
            self.headers["Cookie"] = f"{name}={cookie['Value']}"
        if connection.get("apikey"):
            self.headers["ApiKey"] = str(connection["apikey"])

        self.log = logger

    # -- transport ---------------------------------------------------------

    def call(
        self,
        query: str,
        variables: Optional[dict] = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        operation: str = "query",
        attempts: int = MAX_ATTEMPTS,
    ) -> dict:
        """Execute a GraphQL document.

        Transport failures are retried with backoff; GraphQL errors are not, because a
        malformed query or a rejected mutation will fail identically every time.
        """
        payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        last_error: Optional[Exception] = None

        for attempt in range(1, attempts + 1):
            request = urllib.request.Request(
                self.url, data=payload, headers=self.headers, method="POST"
            )
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    raw = response.read(MAX_RESPONSE_BYTES)
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read(8192).decode("utf-8", "replace")
                except Exception:
                    pass
                # 4xx will not improve on retry; 5xx might.
                if 400 <= exc.code < 500:
                    raise StashError(
                        "STASH_HTTP_ERROR",
                        f"Stash returned HTTP {exc.code} for {operation}.",
                        {"status": exc.code, "body": body[:500], "operation": operation},
                    ) from None
                last_error = exc
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
                last_error = exc
            else:
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise StashError(
                        "STASH_BAD_RESPONSE",
                        f"Stash returned a response that is not JSON: {exc}",
                        {"operation": operation},
                    ) from None

                if parsed.get("errors"):
                    messages = "; ".join(
                        str(error.get("message", error)) for error in parsed["errors"]
                    )
                    raise StashError(
                        "STASH_GRAPHQL_ERROR",
                        f"Stash rejected {operation}: {messages}",
                        {"operation": operation, "errors": parsed["errors"][:5]},
                    )
                return parsed.get("data") or {}

            if attempt < attempts:
                delay = BACKOFF_SECONDS ** attempt
                if self.log:
                    self.log.warning(
                        f"{operation} failed ({last_error}); retrying in {delay:.1f}s "
                        f"(attempt {attempt + 1} of {attempts})"
                    )
                time.sleep(delay)

        raise StashError(
            "STASH_UNREACHABLE",
            f"Could not reach Stash at {self.url} after {attempts} attempts: {last_error}",
            {"url": self.url, "operation": operation},
        )

    # -- reads -------------------------------------------------------------

    def version(self) -> str:
        data = self.call("query { version { version } }", operation="version")
        return ((data.get("version") or {}).get("version")) or ""

    def library_paths(self) -> list[str]:
        """Stash's configured library roots, for `G_LIBRARY_SCOPE`."""
        data = self.call(
            "query { configuration { general { stashes { path } } } }",
            operation="configuration",
        )
        general = ((data.get("configuration") or {}).get("general")) or {}
        return [
            entry["path"]
            for entry in (general.get("stashes") or [])
            if entry and entry.get("path")
        ]

    def scene_count(self) -> int:
        """Total scenes, used as the denominator for `maxFractionOfLibrary`."""
        data = self.call(
            "query { findScenes(filter: {per_page: 1}) { count } }", operation="sceneCount"
        )
        return int(((data.get("findScenes") or {}).get("count")) or 0)

    def duplicate_groups(self, distance: int, duration_diff: float) -> list[list[dict]]:
        query = """
        query FindDuplicateScenes($distance: Int, $duration_diff: Float) {
          findDuplicateScenes(distance: $distance, duration_diff: $duration_diff) {
            %s
          }
        }
        """ % SCENE_FRAGMENT
        data = self.call(
            query,
            {"distance": distance, "duration_diff": duration_diff},
            timeout=600.0,  # phash comparison over a large library is slow
            # Not retried: three attempts at a ten-minute query turns one slow failure
            # into half an hour of the operator staring at a stalled job.
            attempts=1,
            operation="findDuplicateScenes",
        )
        return data.get("findDuplicateScenes") or []

    def scenes_missing_phash(self) -> int:
        """Scenes with no phash cannot appear in any duplicate group.

        Surfaced in the report so a suspiciously small result set is explained rather
        than mistaken for a clean library.
        """
        data = self.call(
            'query { findScenes(scene_filter: {is_missing: "phash"}, filter: {per_page: 1})'
            " { count } }",
            operation="scenesMissingPhash",
        )
        return int(((data.get("findScenes") or {}).get("count")) or 0)

    # -- writes ------------------------------------------------------------

    def delete_files(self, file_ids: list[str]) -> bool:
        """Remove files from disk and from the database, leaving their scenes intact."""
        if not file_ids:
            return True
        data = self.call(
            "mutation DeleteFiles($ids: [ID!]!) { deleteFiles(ids: $ids) }",
            {"ids": file_ids},
            operation="deleteFiles",
        )
        return bool(data.get("deleteFiles"))

    def destroy_scenes(
        self, scene_ids: list[str], *, delete_file: bool, delete_generated: bool
    ) -> bool:
        """Destroy scenes. `delete_generated` is controlled by `G_GENERATED`."""
        if not scene_ids:
            return True
        data = self.call(
            """
            mutation ScenesDestroy($ids: [ID!]!, $delete_file: Boolean,
                                   $delete_generated: Boolean) {
              scenesDestroy(input: {ids: $ids, delete_file: $delete_file,
                                    delete_generated: $delete_generated})
            }
            """,
            {"ids": scene_ids, "delete_file": delete_file, "delete_generated": delete_generated},
            operation="scenesDestroy",
        )
        return bool(data.get("scenesDestroy"))

    def merge_scenes(
        self, source_ids: list[str], destination_id: str, values: Optional[dict] = None
    ) -> dict:
        """Fold the source scenes into the destination.

        Two things about `sceneMerge` that are not obvious and that dictate how this is
        used, both verified against Stash's source:

        - It copies **no** metadata of its own. Only markers, files, and optionally play
          and o history migrate; tags, performers, rating, studio, urls and the rest are
          applied solely from `values`. Calling it without `values` silently discards
          exactly the curation this is supposed to preserve.
        - It **destroys the source scene rows** and moves their files onto the
          destination, without touching the files on disk. So a following
          `scenesDestroy(source, delete_file: true)` cannot work — the scene is gone, the
          mutation errors, and the video file survives attached to the keeper. The file
          has to be removed with `deleteFiles` instead.
        """
        data = self.call(
            """
            mutation SceneMerge($source: [ID!]!, $destination: ID!,
                                $values: SceneUpdateInput) {
              sceneMerge(input: {source: $source, destination: $destination,
                                 values: $values, play_history: true, o_history: true}) {
                id
                o_counter
                play_count
                rating100
                urls
                tags { id }
                performers { id }
                files { id path }
              }
            }
            """,
            {
                "source": source_ids,
                "destination": destination_id,
                "values": values or {"id": destination_id},
            },
            timeout=MERGE_TIMEOUT,
            operation="sceneMerge",
        )
        return data.get("sceneMerge") or {}

    def set_primary_file(self, scene_id: str, file_id: str) -> bool:
        data = self.call(
            """
            mutation SetPrimary($id: ID!, $file: ID!) {
              sceneUpdate(input: {id: $id, primary_file_id: $file}) { id }
            }
            """,
            {"id": scene_id, "file": file_id},
            operation="sceneUpdate",
        )
        return bool(data.get("sceneUpdate"))

    def find_or_create_tag(self, name: str) -> str:
        data = self.call(
            'query FindTag($name: String!) { findTags(tag_filter: {name: '
            '{value: $name, modifier: EQUALS}}, filter: {per_page: 1}) { tags { id name } } }',
            {"name": name},
            operation="findTags",
        )
        tags = ((data.get("findTags") or {}).get("tags")) or []
        if tags:
            return str(tags[0]["id"])
        created = self.call(
            "mutation CreateTag($name: String!) { tagCreate(input: {name: $name}) { id } }",
            {"name": name},
            operation="tagCreate",
        )
        tag = created.get("tagCreate") or {}
        if not tag.get("id"):
            raise StashError("TAG_CREATE_FAILED", f"Could not create tag '{name}'.")
        return str(tag["id"])

    def add_tag(self, scene_ids: list[str], tag_id: str) -> bool:
        """Additive tagging, so existing scene tags survive."""
        return self._bulk_tag(scene_ids, tag_id, "ADD")

    def remove_tag(self, scene_ids: list[str], tag_id: str) -> bool:
        return self._bulk_tag(scene_ids, tag_id, "REMOVE")

    def _bulk_tag(self, scene_ids: list[str], tag_id: str, mode: str) -> bool:
        """Tag scenes in batches.

        A single mutation covering hundreds of scenes reliably exceeds the request
        timeout, because every updated scene fires `Scene.Update.Post` hooks and any
        other plugin subscribed to them runs synchronously inside this call. Batching
        keeps each request short and stops one slow hook from failing the whole run.
        """
        if not scene_ids:
            return True

        ordered = sorted(set(scene_ids))
        for start in range(0, len(ordered), TAG_BATCH_SIZE):
            batch = ordered[start : start + TAG_BATCH_SIZE]
            data = self.call(
                """
                mutation BulkTag($ids: [ID!]!, $tag: ID!, $mode: BulkUpdateIdMode!) {
                  bulkSceneUpdate(input: {ids: $ids, tag_ids: {ids: [$tag], mode: $mode}}) { id }
                }
                """,
                {"ids": batch, "tag": tag_id, "mode": mode},
                timeout=BULK_TIMEOUT,
                operation=f"bulkSceneUpdate({mode}, {len(batch)} scenes)",
            )
            if data.get("bulkSceneUpdate") is None:
                raise StashError(
                    "BULK_TAG_FAILED",
                    f"bulkSceneUpdate({mode}) returned nothing for a batch of "
                    f"{len(batch)} scenes.",
                    {"mode": mode, "batch_size": len(batch)},
                )
            if self.log:
                done = min(start + TAG_BATCH_SIZE, len(ordered))
                self.log.debug(f"tagged {done}/{len(ordered)} scenes ({mode})")
        return True

    def plugin_settings(self, plugin_id: str) -> dict:
        """This plugin's saved settings.

        A plugin with nothing saved is omitted from the map entirely, so an empty result
        means "all defaults", not an error.
        """
        data = self.call(
            "query PluginConfig($ids: [ID!]) { configuration { plugins(include: $ids) } }",
            {"ids": [plugin_id]},
            operation="pluginConfiguration",
        )
        plugins = ((data.get("configuration") or {}).get("plugins")) or {}
        return plugins.get(plugin_id) or {}

    def configure_plugin(self, plugin_id: str, settings: dict) -> dict:
        """Write plugin settings.

        `configurePlugin` **overwrites the entire configuration** for the plugin — there
        is no merge mode. Callers must pass the complete map, or every setting absent
        from it is silently dropped back to its default. Dropping `protectedPaths` that
        way would remove protections the operator is relying on, so this is never called
        with a partial map.
        """
        data = self.call(
            "mutation ConfigurePlugin($id: ID!, $input: Map!) { "
            "configurePlugin(plugin_id: $id, input: $input) }",
            {"id": plugin_id, "input": settings},
            operation="configurePlugin",
        )
        return data.get("configurePlugin") or {}

    def scenes_with_tag(self, tag_id: str) -> list[str]:
        data = self.call(
            """
            query TaggedScenes($tag: ID!) {
              findScenes(scene_filter: {tags: {value: [$tag], modifier: INCLUDES}},
                         filter: {per_page: -1}) { scenes { id } }
            }
            """,
            {"tag": tag_id},
            operation="findScenes(tagged)",
        )
        return [str(s["id"]) for s in ((data.get("findScenes") or {}).get("scenes") or [])]
