"""
One-time (re-run-as-needed) merge of the tar1090-db aircraft type/registration
shard files into a single flat JSON lookup, keyed by full 6-character ICAO
hex address.

Run manually on the Pi:
    python3 scripts/build_aircraft_type_db.py

Not part of the live app -- app/main.py just reads the merged output file
(AIRCRAFT_TYPES_PATH there, OUTPUT_PATH here -- keep them in sync). Re-run
this whenever tar1090-db is updated on the Pi to pick up new entries.

Shard format (confirmed against the real tar1090-db layout used by tar1090):
each shard is a flat JSON object, GZIP-COMPRESSED despite the ".js" file
extension (the browser-side loader gunzips it in JS -- we do the same here
in Python via the gzip module, detected by magic bytes rather than trusting
the extension, since a plain .json or .json.gz shard would also be valid
input). Each shard's keys are either:
  - the full 6-char ICAO hex ("a1b2c3"), or
  - a suffix of it, with the leading characters implied by the shard's own
    path (e.g. a shard at db/a1/b2.js holding key "c3" implies hex "a1b2c3").

Confirmed on the real Pi: the db/ directory also holds a handful of files
that aren't hex-keyed shards at all (e.g. icao_aircraft_types.js,
airport-coords.js) -- same .js-but-possibly-gzipped treatment applies, but
their top-level JSON isn't a flat {hex: [...]} object (one was a list,
which crashed an earlier version of this script on `.items()`). Anything
whose parsed top-level JSON isn't a dict is skipped with a warning naming
the file, rather than assumed to be a shard.

We don't replicate tar1090's JS tree-walk logic to figure out the prefix
depth -- instead, for each shard we derive a path-based prefix from its
location on disk (directory names + filename, hex characters only) and
combine it with each key: if the key is already 6 hex chars, it's a full
hex on its own; otherwise we prepend just enough of the path-derived prefix
to reach 6 characters. This works regardless of how many directory levels
deep a given shard sits.

Each value is [registration, type_designator, flag_code, description] --
we keep registration/type_designator/description (flag_code isn't used
anywhere downstream, so it's dropped to keep the merged file lean).
"""
import gzip
import json
import os
import sys

TAR1090_DB_PATH = "/usr/local/share/tar1090/git-db/db"
OUTPUT_PATH = "/home/david/birdnet/data/aircraft_types.json"

HEX_CHARS = set("0123456789abcdefABCDEF")
FULL_HEX_LEN = 6
GZIP_MAGIC = b"\x1f\x8b"
SKIP_SAMPLE_LIMIT = 10  # how many example skipped keys to print per category


def _read_shard_json(path):
    """Read one shard file's content as a parsed JSON dict, transparently
    handling gzip-compressed content regardless of file extension."""
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:2] == GZIP_MAGIC:
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def _path_prefix(root, path):
    """Derive the hex prefix a shard implies from its location on disk:
    every directory component plus the filename stem, concatenated,
    lowercased. E.g. root/a1/b2.js -> "a1b2".

    Each path component is truncated at its first non-hex character rather
    than having non-hex characters filtered out of it -- real tar1090-db
    shard names are pure hex with no descriptive suffixes, but a stray
    hex-lookalike letter (a-f) inside a non-hex suffix on some other file
    must not get stitched into the prefix out of its original position."""
    rel = os.path.relpath(path, root)
    rel_no_ext = rel
    for ext in (".js", ".json.gz", ".json", ".gz"):
        if rel_no_ext.endswith(ext):
            rel_no_ext = rel_no_ext[: -len(ext)]
            break
    parts = rel_no_ext.replace(os.sep, "/").split("/")
    prefix = ""
    for part in parts:
        part = part.lower()
        hex_run = ""
        for c in part:
            if c not in "0123456789abcdef":
                break
            hex_run += c
        prefix += hex_run
    return prefix


def _resolve_hex(prefix, key):
    """Combine a shard's path-derived prefix with one of its JSON keys to
    get a full 6-char ICAO hex, or None if it can't be resolved cleanly."""
    key = key.lower()
    if not all(c in "0123456789abcdef" for c in key):
        return None
    if len(key) == FULL_HEX_LEN:
        return key
    if len(key) < FULL_HEX_LEN:
        needed = FULL_HEX_LEN - len(key)
        if len(prefix) >= needed:
            return (prefix[:needed] + key)[:FULL_HEX_LEN]
    return None


def find_shards(root):
    shards = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith((".js", ".json", ".json.gz", ".gz")):
                shards.append(os.path.join(dirpath, name))
    return sorted(shards)


def merge(tar1090_db_path=TAR1090_DB_PATH):
    if not os.path.isdir(tar1090_db_path):
        print(f"[ERROR] {tar1090_db_path} does not exist or is not a directory", file=sys.stderr)
        sys.exit(1)

    shards = find_shards(tar1090_db_path)
    print(f"Found {len(shards)} shard file(s) under {tar1090_db_path}")

    merged = {}
    skipped_unresolvable_prefix = 0
    skipped_malformed_value = 0
    unresolvable_samples = []
    malformed_samples = []
    failed_shards = 0
    skipped_shards = 0
    for shard_path in shards:
        try:
            data = _read_shard_json(shard_path)
        except Exception as e:
            print(f"[WARN] could not read {shard_path}: {e}")
            failed_shards += 1
            continue

        # The db/ directory also holds a few non-shard files alongside the
        # actual hex-keyed shards -- e.g. icao_aircraft_types.js (a
        # type-designator -> description lookup) and airport-coords.js,
        # which are JSON but not flat {hex: [...]} dicts (some are lists).
        # Skip anything that isn't a dict at the top level rather than
        # crashing on .items().
        if not isinstance(data, dict):
            print(f"[WARN] skipping {shard_path}: top-level JSON is {type(data).__name__}, not an object -- not a hex-keyed shard")
            skipped_shards += 1
            continue

        prefix = _path_prefix(tar1090_db_path, shard_path)
        for key, value in data.items():
            full_hex = _resolve_hex(prefix, key)
            if full_hex is None:
                skipped_unresolvable_prefix += 1
                if len(unresolvable_samples) < SKIP_SAMPLE_LIMIT:
                    unresolvable_samples.append((shard_path, prefix, key, value))
                continue
            if not isinstance(value, list) or len(value) < 2:
                skipped_malformed_value += 1
                if len(malformed_samples) < SKIP_SAMPLE_LIMIT:
                    malformed_samples.append((shard_path, prefix, key, value))
                continue
            registration = value[0] or None
            type_designator = value[1] or None
            description = value[3] if len(value) > 3 and value[3] else None
            merged[full_hex] = {
                "registration": registration,
                "type_designator": type_designator,
                "description": description,
            }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(merged, f)

    print(f"Merged {len(merged)} aircraft type entries -> {OUTPUT_PATH}")
    if skipped_unresolvable_prefix:
        print(f"  ({skipped_unresolvable_prefix} keys skipped -- unresolvable hex prefix)")
        print(f"  sample unresolvable-prefix keys (up to {SKIP_SAMPLE_LIMIT}):")
        for shard_path, prefix, key, value in unresolvable_samples:
            print(f"    shard={shard_path} prefix={prefix!r} key={key!r} value={value!r}")
    if skipped_malformed_value:
        print(f"  ({skipped_malformed_value} keys skipped -- malformed value)")
        print(f"  sample malformed-value entries (up to {SKIP_SAMPLE_LIMIT}):")
        for shard_path, prefix, key, value in malformed_samples:
            print(f"    shard={shard_path} prefix={prefix!r} key={key!r} value={value!r}")
    if skipped_shards:
        print(f"  ({skipped_shards} non-shard file(s) skipped -- not a hex-keyed JSON object)")
    if failed_shards:
        print(f"  ({failed_shards} shard file(s) failed to read)")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else TAR1090_DB_PATH
    merge(path)
