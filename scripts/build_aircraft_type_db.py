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

Confirmed on the real Pi, two categories of non-aircraft content live
alongside the real shards under db/, both now handled explicitly rather
than falling into the generic skip-and-log path:
  - "children" keys: an intermediate/routing shard can itself contain a
    literal "children" key whose value lists its own sub-shard filenames
    (tree-navigation metadata tar1090's JS loader uses to walk deeper --
    we don't need it since we walk the whole directory tree ourselves).
    This isn't malformed data, so it's skipped silently and counted
    separately, not lumped in with genuine unresolvable-prefix skips.
  - Non-shard reference files: db/ also holds a handful of JSON files that
    aren't hex-keyed aircraft shards at all -- confirmed examples include
    files.js (a JSON list), regIcao.js (index -> hex lookup),
    airport-coords.js (airport code -> [lat, lon]), icao_aircraft_types.js
    and icao_aircraft_types2.js (type-code -> description reference
    tables, two different shapes), operators.js (ICAO operator code ->
    operator info -- a separate lookup from our own airline_codes.py) and
    ranges.js (named hex block ranges, e.g. "military"). New ones keep
    turning up, so rather than growing a filename denylist forever, these
    are excluded structurally: every genuine shard file observed so far
    is named with a filename stem (before its extension) that is pure hex
    characters only (0-9, A-F case-insensitive) -- e.g. 3.js, A8.js,
    A95.js, AA3.js -- while every non-shard file has an actual word in
    its name. Any file whose stem isn't pure hex is treated as a known
    non-shard file and skipped before it's even read.

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

Second output -- type-designator name lookup:
Some hex shard entries have a type_designator (e.g. "C68A", "CL35") but no
description, so the app has nothing friendly to show for them. The two
excluded reference files above -- icao_aircraft_types.js and
icao_aircraft_types2.js -- are keyed by that same type_designator instead
of by hex, so while we're already walking past them for the primary merge,
we also harvest a second lookup from them: type_designator -> human name.
The two files use different shapes (confirmed real on the Pi):
  - icao_aircraft_types2.js: {"A002": ["IRKUT A-002", "G1P", "L"]} -- the
    first array element is a real "Manufacturer Model" name. Used as the
    primary source.
  - icao_aircraft_types.js: {"A002": {"desc": "G1P", "wtc": "L"}} -- "desc"
    here is a short ICAO aircraft description code (engine count/type/wing
    configuration), not a full name -- it's the only text this file offers
    per designator, so it's used only as a last-resort fallback when
    icao_aircraft_types2.js doesn't have the same designator.
This second lookup is written to TYPE_NAMES_OUTPUT_PATH, independent of
the primary hex-keyed merge above (app/main.py reads both files).
"""
import gzip
import json
import os
import sys

TAR1090_DB_PATH = "/usr/local/share/tar1090/git-db/db"
OUTPUT_PATH = "/home/david/birdnet/data/aircraft_types.json"
TYPE_NAMES_OUTPUT_PATH = "/home/david/birdnet/data/aircraft_type_names.json"

HEX_CHARS = set("0123456789abcdefABCDEF")
FULL_HEX_LEN = 6
GZIP_MAGIC = b"\x1f\x8b"

# Known filename extensions a shard (or a non-shard reference file) can
# carry -- stripped off before checking whether the remaining stem is pure
# hex. Order matters: ".json.gz" must be checked before ".gz"/".json".
KNOWN_EXTENSIONS = (".js", ".json.gz", ".json", ".gz")

# Real, confirmed non-shard filenames under db/, purely for documentation
# in error/debug output -- the actual exclusion is structural (see
# _is_hex_shard_filename), not a lookup against this set.
KNOWN_NON_SHARD_FILENAME_EXAMPLES = (
    "files.js", "regIcao.js", "airport-coords.js", "icao_aircraft_types.js",
    "icao_aircraft_types2.js", "operators.js", "ranges.js",
)

# These two non-shard files ARE actively read, for the second (type-name)
# output -- see module docstring. icao_aircraft_types2.js entries win on
# conflict; icao_aircraft_types.js is a fallback-only source.
TYPE_NAME_REFERENCE_FILENAMES = {"icao_aircraft_types.js", "icao_aircraft_types2.js"}

# tar1090's own tree-navigation key, not an aircraft record -- see docstring.
CHILDREN_KEY = "children"

# Diagnostics: keep up to this many total sample rows per skip category,
# capped at SKIP_SAMPLE_PER_SHARD from any single shard so one repetitive
# shard can't fill the whole sample with near-identical examples and hide
# a different, real pattern elsewhere in the data.
SKIP_SAMPLE_LIMIT = 20
SKIP_SAMPLE_PER_SHARD = 1


def _read_shard_json(path):
    """Read one shard file's content as a parsed JSON dict, transparently
    handling gzip-compressed content regardless of file extension."""
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:2] == GZIP_MAGIC:
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def _strip_known_extension(name):
    """Strip one trailing known extension (see KNOWN_EXTENSIONS) off a
    filename, leaving its stem. No-op if none match."""
    for ext in KNOWN_EXTENSIONS:
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def _is_hex_shard_filename(filename):
    """True if `filename`'s stem (extension stripped) is non-empty and
    made up entirely of hex characters (0-9, A-F case-insensitive) --
    the pattern every genuine tar1090-db shard file observed so far
    follows (3.js, A8.js, A95.js, AA3.js, ...), as opposed to the
    reference/lookup files living alongside them under db/, which all
    have an actual word in their name (files.js, regIcao.js,
    airport-coords.js, ...). See module docstring."""
    stem = _strip_known_extension(filename)
    return bool(stem) and all(c in HEX_CHARS for c in stem)


def _extract_type_name(filename, value):
    """Pull a human-readable name out of one entry of icao_aircraft_types.js
    or icao_aircraft_types2.js -- see module docstring for the two shapes.
    Returns None if `value` doesn't match the shape expected for that
    filename (defensive: real data should always match, but a malformed or
    future-format entry should be skipped, not crash the run)."""
    if filename == "icao_aircraft_types2.js":
        if isinstance(value, (list, tuple)) and value and value[0]:
            return str(value[0])
        return None
    if filename == "icao_aircraft_types.js":
        if isinstance(value, dict) and value.get("desc"):
            return str(value["desc"])
        return None
    return None


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
    rel_no_ext = _strip_known_extension(rel)
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


class _SampleCollector:
    """Keeps up to SKIP_SAMPLE_LIMIT sample rows total, at most
    SKIP_SAMPLE_PER_SHARD from any single shard, so a diagnostic dump
    reflects the variety of shards actually producing skips rather than
    just the first shard (alphabetically) that happens to hit the cap."""

    def __init__(self):
        self.rows = []
        self._per_shard_counts = {}

    def add(self, shard_path, prefix, key, value):
        if len(self.rows) >= SKIP_SAMPLE_LIMIT:
            return
        count = self._per_shard_counts.get(shard_path, 0)
        if count >= SKIP_SAMPLE_PER_SHARD:
            return
        self.rows.append((shard_path, prefix, key, value))
        self._per_shard_counts[shard_path] = count + 1


def merge(tar1090_db_path=TAR1090_DB_PATH):
    if not os.path.isdir(tar1090_db_path):
        print(f"[ERROR] {tar1090_db_path} does not exist or is not a directory", file=sys.stderr)
        sys.exit(1)

    shards = find_shards(tar1090_db_path)
    print(f"Found {len(shards)} shard file(s) under {tar1090_db_path}")

    merged = {}
    skipped_unresolvable_prefix = 0
    skipped_malformed_value = 0
    skipped_children_keys = 0
    unresolvable_samples = _SampleCollector()
    malformed_samples = _SampleCollector()
    failed_shards = 0
    skipped_non_dict_shards = 0
    skipped_non_hex_filenames = 0

    # Second output: type_designator -> human name, harvested from the two
    # reference files below while we're already walking past them for the
    # primary merge -- see module docstring. type_names_primary wins ties.
    type_names_primary = {}
    type_names_fallback = {}

    for shard_path in shards:
        basename = os.path.basename(shard_path)
        if not _is_hex_shard_filename(basename):
            if basename in TYPE_NAME_REFERENCE_FILENAMES:
                try:
                    ref_data = _read_shard_json(shard_path)
                except Exception as e:
                    print(f"[WARN] could not read {shard_path}: {e}")
                else:
                    if isinstance(ref_data, dict):
                        target = type_names_primary if basename == "icao_aircraft_types2.js" else type_names_fallback
                        for designator, value in ref_data.items():
                            name = _extract_type_name(basename, value)
                            if name:
                                target[designator] = name
                    else:
                        print(f"[WARN] skipping {shard_path}: top-level JSON is {type(ref_data).__name__}, not an object")
            skipped_non_hex_filenames += 1
            continue

        try:
            data = _read_shard_json(shard_path)
        except Exception as e:
            print(f"[WARN] could not read {shard_path}: {e}")
            failed_shards += 1
            continue

        # A few files under db/ are JSON but not flat {hex: [...]} shards
        # at all (e.g. some parse to a list). Skip anything that isn't a
        # dict at the top level rather than crashing on .items().
        if not isinstance(data, dict):
            print(f"[WARN] skipping {shard_path}: top-level JSON is {type(data).__name__}, not an object -- not a hex-keyed shard")
            skipped_non_dict_shards += 1
            continue

        prefix = _path_prefix(tar1090_db_path, shard_path)
        for key, value in data.items():
            if key == CHILDREN_KEY:
                skipped_children_keys += 1
                continue
            full_hex = _resolve_hex(prefix, key)
            if full_hex is None:
                skipped_unresolvable_prefix += 1
                unresolvable_samples.add(shard_path, prefix, key, value)
                continue
            if not isinstance(value, list) or len(value) < 2:
                skipped_malformed_value += 1
                malformed_samples.add(shard_path, prefix, key, value)
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

    # icao_aircraft_types2.js names win on conflict; icao_aircraft_types.js
    # only fills in designators the primary source didn't have.
    type_names = {**type_names_fallback, **type_names_primary}
    os.makedirs(os.path.dirname(TYPE_NAMES_OUTPUT_PATH), exist_ok=True)
    with open(TYPE_NAMES_OUTPUT_PATH, "w") as f:
        json.dump(type_names, f)

    print(f"Merged {len(merged)} aircraft type entries -> {OUTPUT_PATH}")
    print(f"Merged {len(type_names)} type-designator -> name entries -> {TYPE_NAMES_OUTPUT_PATH}")
    print(f"  ({len(type_names_primary)} from icao_aircraft_types2.js, {len(type_names_fallback)} from icao_aircraft_types.js as fallback-only source)")
    if skipped_unresolvable_prefix:
        print(f"  ({skipped_unresolvable_prefix} keys skipped -- unresolvable hex prefix)")
        print(f"  sample unresolvable-prefix keys (up to {SKIP_SAMPLE_LIMIT}, max {SKIP_SAMPLE_PER_SHARD} per shard):")
        for shard_path, prefix, key, value in unresolvable_samples.rows:
            print(f"    shard={shard_path} prefix={prefix!r} key={key!r} value={value!r}")
    if skipped_malformed_value:
        print(f"  ({skipped_malformed_value} keys skipped -- malformed value)")
        print(f"  sample malformed-value entries (up to {SKIP_SAMPLE_LIMIT}, max {SKIP_SAMPLE_PER_SHARD} per shard):")
        for shard_path, prefix, key, value in malformed_samples.rows:
            print(f"    shard={shard_path} prefix={prefix!r} key={key!r} value={value!r}")
    if skipped_children_keys:
        print(f"  ({skipped_children_keys} \"children\" tree-navigation keys skipped -- expected structure, not an error)")
    if skipped_non_hex_filenames:
        print(f"  ({skipped_non_hex_filenames} non-shard reference file(s) excluded (filename stem isn't pure hex), e.g. {list(KNOWN_NON_SHARD_FILENAME_EXAMPLES)})")
    if skipped_non_dict_shards:
        print(f"  ({skipped_non_dict_shards} other file(s) skipped -- not a hex-keyed JSON object)")
    if failed_shards:
        print(f"  ({failed_shards} shard file(s) failed to read)")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else TAR1090_DB_PATH
    merge(path)
