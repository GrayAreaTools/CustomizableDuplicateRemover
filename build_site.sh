#!/bin/bash
# Builds the Stash plugin source index.
#
# Output layout, which is what Stash's "Add source" expects to find:
#   <outdir>/index.yml                        the package list
#   <outdir>/CustomizableDuplicateRemover.zip the plugin, files at the zip root
#
# Adapted from stashapp/plugins-repo-template. The upstream script scans ./plugins
# with one directory per plugin; this repo keeps its single plugin in ./plugin, so
# only the search root differs. Plugin id comes from the manifest filename either way.

set -euo pipefail

outdir="${1:-_site}"

rm -rf "$outdir"
mkdir -p "$outdir"

# Resolved once, here, because zip needs an absolute target while cwd is the plugin
# directory. Avoids realpath, which rejects a not-yet-created file on BSD/macOS.
outdir_abs=$(cd "$outdir" && pwd)

buildPlugin() {
    f=$1

    if grep -q "^#pkgignore" "$f"; then
        return
    fi

    dir=$(dirname "$f")
    plugin_id=$(basename "$f" .yml)

    echo "Processing $plugin_id"

    # Version is the manifest version plus the short hash of the last commit to
    # touch the plugin, so every published build is distinguishable. Date is forced
    # to UTC rather than the runner's locale.
    rev=$(git log -n 1 --pretty=format:%h -- "$dir"/*)
    updated=$(TZ=UTC0 git log -n 1 --date="format-local:%F %T" --pretty=format:%ad -- "$dir"/*)

    zipfile="$outdir_abs/$plugin_id.zip"

    # Run artifacts and bytecode are local state, never shipped. reports/ in
    # particular would leak the absolute path of every file in the builder's library.
    pushd "$dir" > /dev/null
    zip -r "$zipfile" . -x "reports/*" "__pycache__/*" "*.pyc" > /dev/null
    popd > /dev/null

    name=$(grep "^name:" "$f" | head -n 1 | cut -d' ' -f2- | sed -e 's/\r//' -e 's/^"\(.*\)"$/\1/')
    description=$(grep "^description:" "$f" | head -n 1 | cut -d' ' -f2- | sed -e 's/\r//' -e 's/^"\(.*\)"$/\1/')
    ymlVersion=$(grep "^version:" "$f" | head -n 1 | cut -d' ' -f2- | sed -e 's/\r//' -e 's/^"\(.*\)"$/\1/')
    version="$ymlVersion-$rev"
    dep=$(grep "^# requires:" "$f" | cut -c 12- | sed -e 's/\r//' || true)

    # Values are quoted because a description containing ": " is otherwise parsed as
    # a nested mapping, which is the failure the unconfigured source produced.
    {
        echo "- id: $plugin_id"
        echo "  name: \"$name\""
        echo "  metadata:"
        echo "    description: \"$description\""
        echo "  version: \"$version\""
        echo "  date: \"$updated\""
        echo "  path: $plugin_id.zip"
        echo "  sha256: $(sha256sum "$zipfile" | cut -d' ' -f1)"
    } >> "$outdir"/index.yml

    if [ -n "$dep" ]; then
        echo "  requires:" >> "$outdir"/index.yml
        for d in ${dep//,/ }; do
            echo "    - $d" >> "$outdir"/index.yml
        done
    fi

    echo "" >> "$outdir"/index.yml
}

find ./plugin -mindepth 1 -maxdepth 1 -name '*.yml' | while read -r file; do
    buildPlugin "$file"
done
