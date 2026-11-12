#!/usr/bin/env python3
"""pathdelta - list what changed between two JSON documents, by path.

One command, one job: read two documents and print the smallest set of
path-addressed changes that turns the first into the second. Arrays are
aligned by element value, so inserting one element does not turn every
element after it into a reported change.

Standard library only. No configuration file, no install step.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from bisect import bisect_left
from typing import NamedTuple

# Sentinel for a path that does not exist on one side.
MISSING = object()

# Work bounds. An alignment window larger than this is compared by position
# instead of by value; keeps a badly shuffled huge array from stalling the run.
MAX_ALIGN_CELLS = 1_000_000
MAX_PAIR_CELLS = 20_000

_SIMPLE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Change(NamedTuple):
    tokens: tuple
    path: str
    op: str  # "add" | "remove" | "change"
    before: object
    after: object
    why: str


class Result(NamedTuple):
    changes: list
    notes: list
    arrays: int
    matched: int


class MarkedDict(dict):
    """A dict that remembers keys written more than once in the source text."""

    def __init__(self, pairs):
        super().__init__()
        duplicates = []
        for key, value in pairs:
            if key in self:
                duplicates.append(key)
            self[key] = value
        self.duplicates = duplicates


def fingerprint(value):
    """A canonical string for a JSON value. This is the unit of equality."""
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False,
    )


def kind(value):
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, bool):
        return "bool"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return "number"
    return "string"


def child_path(base, token):
    if isinstance(token, int):
        return "%s[%d]" % (base, token)
    if _SIMPLE_KEY.match(token):
        return "%s.%s" % (base, token)
    return "%s[%s]" % (base, json.dumps(token, ensure_ascii=False))


def render(tokens):
    path = "$"
    for token in tokens:
        path = child_path(path, token)
    return path


def compile_ignore(pattern):
    """Turn a path pattern into a matcher for that path and everything below it.

    ``[*]`` matches any array index. A trailing ``.*`` is accepted and has the
    same meaning as the bare path (a path already covers its subtree).
    """
    if not pattern.startswith("$"):
        raise ValueError("--ignore must start with $")
    text = pattern.strip()
    if text.endswith(".*"):
        text = text[:-2]
    pieces = text.split("[*]")
    body = r"\[\d+\]".join(re.escape(piece) for piece in pieces)
    return re.compile(body + r"(?:[.\[].*)?")


def leaf_count(value):
    if isinstance(value, dict):
        return sum(leaf_count(item) for item in value.values()) or 1
    if isinstance(value, list):
        return sum(leaf_count(item) for item in value) or 1
    return 1


def shared_leaves(left, right):
    """How many leaf values agree at the same position in two values."""
    if kind(left) != kind(right):
        return 0
    if isinstance(left, dict):
        return sum(
            shared_leaves(left[key], right[key])
            for key in left.keys() & right.keys()
        )
    if isinstance(left, list):
        return sum(shared_leaves(a, b) for a, b in zip(left, right))
    return 1 if fingerprint(left) == fingerprint(right) else 0


def similarity(left, right):
    if fingerprint(left) == fingerprint(right):
        return 1.0
    denominator = max(leaf_count(left), leaf_count(right), 1)
    return shared_leaves(left, right) / denominator


def longest_block(left, right, a0, a1, b0, b1):
    """Longest contiguous run of equal fingerprints inside two windows."""
    width = b1 - b0
    previous = [0] * (width + 1)
    best_i, best_j, best_len = a0, b0, 0
    for i in range(a0, a1):
        current = [0] * (width + 1)
        item = left[i]
        for j in range(width):
            if item == right[b0 + j]:
                current[j + 1] = previous[j] + 1
                if current[j + 1] > best_len:
                    best_len = current[j + 1]
                    best_i = i - best_len + 1
                    best_j = b0 + j - best_len + 1
        previous = current
    return best_i, best_j, best_len


def anchor_pairs(left, right, a0, a1, b0, b1):
    """Repeatedly pin the longest equal run, then recurse into both sides."""
    pairs = []
    stack = [(a0, a1, b0, b1)]
    while stack:
        x0, x1, y0, y1 = stack.pop()
        if x0 >= x1 or y0 >= y1:
            continue
        i, j, size = longest_block(left, right, x0, x1, y0, y1)
        if size == 0:
            continue
        stack.append((i + size, x1, j + size, y1))
        for offset in range(size):
            pairs.append((i + offset, j + offset))
        stack.append((x0, i, y0, j))
    pairs.sort()
    return pairs


def longest_increasing_subsequence(sequence):
    """Longest subsequence of (i, j) pairs whose j values strictly increase.

    The input is already ordered by i, so the result is a set of matches that
    never cross each other. Standard patience sorting, O(n log n).
    """
    tails = []
    tail_index = []
    parent = [-1] * len(sequence)
    for index, pair in enumerate(sequence):
        j = pair[1]
        slot = bisect_left(tails, j)
        if slot > 0:
            parent[index] = tail_index[slot - 1]
        if slot == len(tails):
            tails.append(j)
            tail_index.append(index)
        else:
            tails[slot] = j
            tail_index[slot] = index
    result = []
    cursor = tail_index[-1] if tail_index else -1
    while cursor >= 0:
        result.append(sequence[cursor])
        cursor = parent[cursor]
    result.reverse()
    return result


def unique_anchors(left, right, a0, a1, b0, b1):
    """Pin values that occur exactly once on each side.

    Such a value has exactly one possible partner, so it is free of guessing.
    A single insert or delete shifts every later index, but these pairs still
    line up; keeping only an increasing subsequence discards the few matches
    that would cross an insertion.
    """
    count_left = {}
    for i in range(a0, a1):
        value = left[i]
        count_left[value] = count_left.get(value, 0) + 1
    positions = {}
    for j in range(b0, b1):
        positions.setdefault(right[j], []).append(j)
    candidates = []
    for i in range(a0, a1):
        value = left[i]
        found = positions.get(value)
        if count_left[value] == 1 and found is not None and len(found) == 1:
            candidates.append((i, found[0]))
    return longest_increasing_subsequence(candidates)


def gaps_of(pairs, n, m):
    gaps = []
    ia, ib = 0, 0
    for x, y in pairs + [(n, m)]:
        if ia < x or ib < y:
            gaps.append((ia, x, ib, y))
        ia, ib = x + 1, y + 1
    return gaps


def align(left, right):
    """Return (matching index pairs, gaps) for two fingerprint lists.

    Three cheap stages, in order:
      1. pin the shared prefix and suffix (the usual insert/delete case),
      2. pin values that are unique on both sides, keeping a non-crossing set,
      3. inside each remaining gap, pin the longest equal runs.
    Whatever is left is a genuine change region, handled by pair_gap.
    """
    n, m = len(left), len(right)
    prefix = 0
    while prefix < min(n, m) and left[prefix] == right[prefix]:
        prefix += 1
    suffix = 0
    while suffix < min(n, m) - prefix and left[n - 1 - suffix] == right[m - 1 - suffix]:
        suffix += 1

    pairs = [(i, i) for i in range(prefix)]
    pairs.extend((n - suffix + i, m - suffix + i) for i in range(suffix))
    pairs.extend(unique_anchors(left, right, prefix, n - suffix,
                                prefix, m - suffix))
    pairs.sort()

    for x0, x1, y0, y1 in gaps_of(pairs, n, m):
        cells = (x1 - x0) * (y1 - y0)
        if 0 < cells <= MAX_ALIGN_CELLS:
            pairs.extend(anchor_pairs(left, right, x0, x1, y0, y1))
    pairs.sort()
    return pairs, gaps_of(pairs, n, m)


def pair_gap(left, right):
    """Pair up elements inside one unaligned gap.

    First greedy: highest similarity wins, ties go to the earliest positions.
    Then anything left is paired by position, so a single scalar edit becomes
    one change instead of a remove plus an add.
    """
    n, m = len(left), len(right)
    pairs = []
    used_a, used_b = set(), set()

    if n * m <= MAX_PAIR_CELLS:
        candidates = []
        for i in range(n):
            for j in range(m):
                candidates.append((similarity(left[i], right[j]), i, j))
        candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
        for score, i, j in candidates:
            if score <= 0:
                break
            if i in used_a or j in used_b:
                continue
            used_a.add(i)
            used_b.add(j)
            pairs.append((i, j, score))

    free_a = [i for i in range(n) if i not in used_a]
    free_b = [j for j in range(m) if j not in used_b]
    while free_a and free_b:
        i, j = free_a.pop(0), free_b.pop(0)
        pairs.append((i, j, 0.0))
        used_a.add(i)
        used_b.add(j)
    return pairs, used_a, used_b


def make(op, tokens, path, before, after, why):
    return Change(tokens, path, op, before, after, why)


class Context:
    def __init__(self, ignore):
        self.ignore = ignore
        self.notes = []
        self.arrays = 0
        self.matched = 0

    def skipped(self, path):
        return any(pattern.fullmatch(path) for pattern in self.ignore)


def diff_values(before, after, tokens, path, ctx):
    if ctx.skipped(path):
        return []
    left_kind, right_kind = kind(before), kind(after)
    if left_kind != right_kind:
        return [make("change", tokens, path, before, after,
                     "value type differs, whole value replaced")]

    if left_kind == "object":
        changes = []
        for key, value in before.items():
            if key not in after:
                changes.append(make(
                    "remove", tokens + (key,), child_path(path, key),
                    value, MISSING, "key exists only in BEFORE"))
            else:
                changes.extend(diff_values(
                    value, after[key], tokens + (key,),
                    child_path(path, key), ctx))
        for key, value in after.items():
            if key not in before:
                changes.append(make(
                    "add", tokens + (key,), child_path(path, key),
                    MISSING, value, "key exists only in AFTER"))
        return changes

    if left_kind == "array":
        return diff_array(before, after, tokens, path, ctx)

    if fingerprint(before) != fingerprint(after):
        return [make("change", tokens, path, before, after,
                     "same path, different value")]
    return []


def diff_array(before, after, tokens, path, ctx):
    ctx.arrays += 1
    left = [fingerprint(item) for item in before]
    right = [fingerprint(item) for item in after]
    pairs, gaps = align(left, right)
    ctx.matched += len(pairs)
    changes = []

    for a0, a1, b0, b1 in gaps:
        left_items = before[a0:a1]
        right_items = after[b0:b1]
        if not left_items:
            for offset, value in enumerate(right_items):
                index = b0 + offset
                changes.append(make(
                    "add", tokens + (index,), child_path(path, index),
                    MISSING, value,
                    "new array element at AFTER[%d], no equal element in BEFORE" % index))
            continue
        if not right_items:
            for offset, value in enumerate(left_items):
                index = a0 + offset
                changes.append(make(
                    "remove", tokens + (index,), child_path(path, index),
                    value, MISSING,
                    "BEFORE[%d] has no equal element in AFTER" % index))
            continue

        matched, used_a, used_b = pair_gap(left_items, right_items)
        for i, j, score in sorted(matched, key=lambda item: item[1]):
            index = b0 + j
            sub_tokens = tokens + (index,)
            sub_path = child_path(path, index)
            if score == 0 and fingerprint(left_items[i]) != fingerprint(right_items[j]):
                changes.append(make(
                    "change", sub_tokens, sub_path,
                    left_items[i], right_items[j],
                    "paired elements share no field; whole element replaced"))
            else:
                changes.extend(diff_values(
                    left_items[i], right_items[j], sub_tokens, sub_path, ctx))
        for i in range(len(left_items)):
            if i not in used_a:
                index = a0 + i
                changes.append(make(
                    "remove", tokens + (index,), child_path(path, index),
                    left_items[i], MISSING,
                    "unpaired BEFORE[%d] element" % index))
        for j in range(len(right_items)):
            if j not in used_b:
                index = b0 + j
                changes.append(make(
                    "add", tokens + (index,), child_path(path, index),
                    MISSING, right_items[j],
                    "unpaired AFTER[%d] element" % index))

    # Array elements are emitted directly rather than through diff_values,
    # so the ignore check has to be applied here as well.
    changes = [item for item in changes if not ctx.skipped(item.path)]
    adds = sum(1 for item in changes if item.op == "add")
    removes = sum(1 for item in changes if item.op == "remove")
    edits = sum(1 for item in changes if item.op == "change")
    ctx.notes.append(
        "align %s | %d matched by value, %d gap(s), +%d -%d ~%d"
        % (path, len(pairs), len(gaps), adds, removes, edits))
    return changes


def sort_key(change):
    return tuple(
        (0, token) if isinstance(token, str) else (1, token)
        for token in change.tokens
    )


def diff(before, after, ignore=()):
    """Public API: return a Result for two decoded JSON values."""
    ctx = Context([compile_ignore(p) for p in ignore])
    changes = diff_values(before, after, (), "$", ctx)
    changes.sort(key=sort_key)
    return Result(changes, ctx.notes, ctx.arrays, ctx.matched)


def duplicate_paths(node, path="$"):
    out = []
    if isinstance(node, MarkedDict):
        for key in sorted(set(node.duplicates)):
            extra = sum(1 for item in node.duplicates if item == key)
            out.append((child_path(path, key), extra))
        for key, value in node.items():
            out.extend(duplicate_paths(value, child_path(path, key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            out.extend(duplicate_paths(value, child_path(path, index)))
    return out


def bad_constant(text):
    raise ValueError("non-standard number %r is not valid JSON" % text)


def decode(text, label):
    if text.startswith("\ufeff"):
        text = text[1:]
    try:
        return json.loads(
            text, object_pairs_hook=MarkedDict, parse_constant=bad_constant)
    except ValueError as exc:
        raise ValueError("%s: %s" % (label, exc)) from exc


def read_document(name):
    if name == "-":
        raw = sys.stdin.buffer.read()
    else:
        with open(name, "rb") as handle:
            raw = handle.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("%s: not UTF-8 (%s)" % (name, exc)) from exc
    return decode(text, name)


def show(value, limit=160):
    if value is MISSING:
        return "(absent)"
    text = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    if len(text) > limit:
        return text[:limit - 3] + "..."
    return text


def human_line(change):
    if change.op == "add":
        return "+ %s: %s" % (change.path, show(change.after))
    if change.op == "remove":
        return "- %s: %s" % (change.path, show(change.before))
    return "~ %s: %s -> %s" % (
        change.path, show(change.before), show(change.after))


def json_payload(changes, explain):
    out = []
    for change in changes:
        record = {"path": change.path, "op": change.op}
        if change.before is not MISSING:
            record["before"] = change.before
        if change.after is not MISSING:
            record["after"] = change.after
        if explain:
            record["why"] = change.why
        out.append(record)
    return out


def build_parser():
    parser = argparse.ArgumentParser(
        prog="pathdelta",
        description="List what changed between two JSON documents, by path.",
        epilog="Exit status: 0 identical, 1 changes found, 2 input error.")
    parser.add_argument("before", help="BEFORE document, or - for standard input")
    parser.add_argument("after", help="AFTER document, or - for standard input")
    parser.add_argument("--json", action="store_true",
                        help="print a JSON array of changes instead of text")
    parser.add_argument("--explain", action="store_true",
                        help="print the reason for every change and every alignment")
    parser.add_argument("--ignore", action="append", default=[], metavar="PATH",
                        help="skip a path and its subtree; [*] matches any index (repeatable)")
    return parser


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)

    try:
        ignore = [compile_ignore(p) for p in args.ignore]
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    try:
        before = read_document(args.before)
        after = read_document(args.after)
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    ctx = Context(ignore)
    changes = diff_values(before, after, (), "$", ctx)
    changes.sort(key=sort_key)
    duplicates = duplicate_paths(before) + duplicate_paths(after)

    if args.json:
        print(json.dumps(json_payload(changes, args.explain),
                         ensure_ascii=False, indent=2))
    else:
        for change in changes:
            print(human_line(change))
            if args.explain:
                print("    why: %s" % change.why)
        adds = sum(1 for c in changes if c.op == "add")
        removes = sum(1 for c in changes if c.op == "remove")
        edits = sum(1 for c in changes if c.op == "change")
        print("%d change(s): %d added, %d removed, %d changed"
              % (len(changes), adds, removes, edits))

    if args.explain and not args.json:
        print("# alignment notes")
        for note in ctx.notes:
            print("# %s" % note)
        for path, count in duplicates:
            print("# note: duplicate key %s written %d times, last value kept"
                  % (path, count + 1))
    elif args.explain:
        for note in ctx.notes:
            print("# %s" % note, file=sys.stderr)
        for path, count in duplicates:
            print("# note: duplicate key %s written %d times, last value kept"
                  % (path, count + 1), file=sys.stderr)
    elif duplicates:
        print("# note: %d duplicate key(s) in the input; run with --explain to see them"
              % len(duplicates), file=sys.stderr)

    return 1 if changes else 0


if __name__ == "__main__":
    sys.exit(main())
