"""Tests for pathdelta. Run: python -m unittest discover -s tests -v"""

import copy
import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pathdelta as pd  # noqa: E402

MAIN = ROOT / "pathdelta.py"


def changes(before, after, ignore=()):
    return pd.diff(before, after, ignore).changes


def ops(before, after, ignore=()):
    return [(c.path, c.op) for c in changes(before, after, ignore)]


def run_cli(*args, stdin=None):
    return subprocess.run(
        [sys.executable, str(MAIN), *args],
        input=stdin, capture_output=True, text=True, encoding="utf-8",
        cwd=str(ROOT),
    )


class Helpers(unittest.TestCase):
    def test_fingerprint_sorts_keys(self):
        self.assertEqual(pd.fingerprint({"b": 1, "a": 2}),
                         pd.fingerprint({"a": 2, "b": 1}))

    def test_fingerprint_separates_int_and_float(self):
        self.assertNotEqual(pd.fingerprint(1), pd.fingerprint(1.0))

    def test_fingerprint_separates_bool_and_int(self):
        self.assertNotEqual(pd.fingerprint(True), pd.fingerprint(1))

    def test_kind_separates_bool_from_number(self):
        self.assertEqual(pd.kind(True), "bool")
        self.assertEqual(pd.kind(1), "number")
        self.assertEqual(pd.kind(None), "null")
        self.assertEqual(pd.kind([]), "array")
        self.assertEqual(pd.kind({}), "object")

    def test_child_path_simple_key(self):
        self.assertEqual(pd.child_path("$", "port"), "$.port")

    def test_child_path_quotes_awkward_keys(self):
        self.assertEqual(pd.child_path("$", "a.b"), '$["a.b"]')
        self.assertEqual(pd.child_path("$", "can not"), '$["can not"]')
        self.assertEqual(pd.child_path("$", "中文"), '$["中文"]')

    def test_render_mixed_tokens(self):
        self.assertEqual(pd.render(("users", 3, "a.b")), '$.users[3]["a.b"]')

    def test_show_truncates_long_values(self):
        text = pd.show("x" * 400)
        self.assertTrue(text.endswith("..."))
        self.assertLessEqual(len(text), 160)

    def test_show_absent(self):
        self.assertEqual(pd.show(pd.MISSING), "(absent)")


class IgnorePatterns(unittest.TestCase):
    def test_pattern_matches_path_and_subtree(self):
        rx = pd.compile_ignore("$.meta")
        self.assertTrue(rx.fullmatch("$.meta"))
        self.assertTrue(rx.fullmatch("$.meta.timestamp"))
        self.assertTrue(rx.fullmatch("$.meta.nested[2]"))
        self.assertFalse(rx.fullmatch("$.metadata"))

    def test_pattern_wildcard_index(self):
        rx = pd.compile_ignore("$.items[*].requestId")
        self.assertTrue(rx.fullmatch("$.items[0].requestId"))
        self.assertTrue(rx.fullmatch("$.items[17].requestId"))
        self.assertFalse(rx.fullmatch("$.items[0].name"))

    def test_pattern_trailing_star_is_optional(self):
        self.assertTrue(pd.compile_ignore("$.a.*").fullmatch("$.a.b"))

    def test_pattern_requires_dollar(self):
        with self.assertRaises(ValueError):
            pd.compile_ignore("meta.timestamp")


class Scalars(unittest.TestCase):
    def test_identical_scalar(self):
        self.assertEqual(changes({"a": 1}, {"a": 1}), [])

    def test_scalar_edit(self):
        got = changes({"a": 1}, {"a": 2})
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0].path, got[0].op), ("$.a", "change"))
        self.assertEqual((got[0].before, got[0].after), (1, 2))

    def test_bool_to_number_is_a_change(self):
        self.assertEqual(ops({"a": True}, {"a": 1}), [("$.a", "change")])

    def test_null_to_string(self):
        self.assertEqual(ops({"a": None}, {"a": "x"}), [("$.a", "change")])

    def test_int_to_float(self):
        self.assertEqual(ops({"a": 1}, {"a": 1.0}), [("$.a", "change")])

    def test_unicode_string_edit(self):
        got = changes({"名称": "旧"}, {"名称": "新"})
        self.assertEqual(got[0].path, '$["名称"]')

    def test_deep_scalar_edit(self):
        self.assertEqual(ops({"a": {"b": {"c": 1}}}, {"a": {"b": {"c": 9}}}),
                         [("$.a.b.c", "change")])

    def test_type_change_replaces_whole_value(self):
        got = changes({"a": {"x": 1}}, {"a": [1, 2]})
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].op, "change")
        self.assertEqual(got[0].after, [1, 2])


class Objects(unittest.TestCase):
    def test_key_order_is_irrelevant(self):
        self.assertEqual(changes({"a": 1, "b": 2}, {"b": 2, "a": 1}), [])

    def test_added_key(self):
        got = changes({"a": 1}, {"a": 1, "b": 2})
        self.assertEqual([(c.path, c.op) for c in got], [("$.b", "add")])
        self.assertIs(got[0].before, pd.MISSING)

    def test_removed_key(self):
        got = changes({"a": 1, "b": 2}, {"a": 1})
        self.assertEqual([(c.path, c.op) for c in got], [("$.b", "remove")])
        self.assertIs(got[0].after, pd.MISSING)

    def test_nested_object_addition(self):
        self.assertEqual(ops({"a": {}}, {"a": {"b": {"c": True}}}),
                         [("$.a.b", "add")])

    def test_quoted_key_in_path(self):
        self.assertEqual(ops({"a.b": 1}, {"a.b": 2}),
                         [('$["a.b"]', "change")])

    def test_empty_object_to_filled(self):
        self.assertEqual(ops({}, {"x": 1, "y": 2}),
                         [("$.x", "add"), ("$.y", "add")])

    def test_output_sorted_by_path(self):
        got = changes({"z": 1, "a": 1}, {"z": 2, "a": 2})
        self.assertEqual([c.path for c in got], ["$.a", "$.z"])

    def test_every_change_has_a_reason(self):
        for c in changes({"a": 1, "b": [1, 2]},
                         {"a": 2, "b": [1, 3], "c": 0}):
            self.assertTrue(c.why)

    def test_tokens_match_rendered_path(self):
        for c in changes({"a": {"b.c": [1]}}, {"a": {"b.c": [2]}}):
            self.assertEqual(pd.render(c.tokens), c.path)


class Arrays(unittest.TestCase):
    def test_append(self):
        self.assertEqual(ops({"v": [1, 2, 3]}, {"v": [1, 2, 3, 4]}),
                         [("$.v[3]", "add")])

    def test_prepend(self):
        self.assertEqual(ops({"v": [1, 2, 3]}, {"v": [0, 1, 2, 3]}),
                         [("$.v[0]", "add")])

    def test_middle_insert_does_not_cascade(self):
        self.assertEqual(ops({"v": [1, 2, 3, 4]},
                             {"v": [1, 2, 99, 3, 4]}),
                         [("$.v[2]", "add")])

    def test_middle_delete(self):
        self.assertEqual(ops({"v": [1, 2, 3, 4]}, {"v": [1, 3, 4]}),
                         [("$.v[1]", "remove")])

    def test_scalar_element_edit_is_one_change(self):
        self.assertEqual(ops({"v": [1, 2, 3]}, {"v": [1, 9, 3]}),
                         [("$.v[1]", "change")])

    def test_reorder_reports_adds_and_removes_not_edits(self):
        got = ops({"v": [1, 2, 3]}, {"v": [3, 2, 1]})
        self.assertEqual(len(got), 4)
        self.assertFalse(any(op == "change" for _, op in got))

    def test_duplicate_elements_align(self):
        self.assertEqual(ops({"v": [1, 1, 2]}, {"v": [1, 1, 2, 1]}),
                         [("$.v[3]", "add")])

    def test_changed_object_plus_insert(self):
        before = {"users": [{"id": 1, "name": "Ann"},
                            {"id": 2, "name": "Bob"}]}
        after = {"users": [{"id": 1, "name": "Anne"},
                           {"id": 3, "name": "Cid"},
                           {"id": 2, "name": "Bob"}]}
        self.assertEqual(ops(before, after),
                         [("$.users[0].name", "change"),
                          ("$.users[1]", "add")])

    def test_unrelated_objects_become_one_whole_value_change(self):
        got = changes({"v": [{"x": 1}]}, {"v": [{"y": 2}]})
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0].path, got[0].op), ("$.v[0]", "change"))

    def test_all_elements_changed_equal_length(self):
        self.assertEqual(ops({"v": [1, 2, 3]}, {"v": [9, 8, 7]}),
                         [("$.v[0]", "change"),
                          ("$.v[1]", "change"),
                          ("$.v[2]", "change")])

    def test_large_array_insert_is_one_change(self):
        before = {"v": list(range(200))}
        after = {"v": before["v"][:100] + [9999] + before["v"][100:]}
        self.assertEqual(ops(before, after), [("$.v[100]", "add")])

    def test_two_inserts(self):
        before = {"v": list(range(20))}
        after = {"v": before["v"][:5] + [101] + before["v"][5:12]
                 + [102] + before["v"][12:]}
        got = ops(before, after)
        self.assertEqual([p for p, _ in got], ["$.v[5]", "$.v[13]"])

    def test_array_to_scalar(self):
        got = changes({"v": [1, 2]}, {"v": 5})
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].op, "change")

    def test_nested_arrays(self):
        self.assertEqual(ops({"v": [[1, 2], [3]]}, {"v": [[1, 2], [3, 4]]}),
                         [("$.v[1][1]", "add")])

    def test_longest_increasing_subsequence_discards_crossing_matches(self):
        seq = [(0, 0), (1, 5), (2, 1), (3, 2), (4, 3)]
        self.assertEqual(pd.longest_increasing_subsequence(seq),
                         [(0, 0), (2, 1), (3, 2), (4, 3)])

    def test_unique_anchors_survive_insert_and_delete(self):
        left = ["v%d" % i for i in range(40)]
        right = left[:10] + ["extra"] + left[10:25] + left[26:]
        got = pd.align(left, right)[0]
        self.assertGreaterEqual(len(got), 37)
        self.assertTrue(all(y > x for x, y in zip(
            [p[1] for p in got], [p[1] for p in got][1:])))

    def test_changed_last_element_does_not_disable_alignment(self):
        before = {"v": [{"id": i, "ok": True} for i in range(60)]}
        after = {"v": [dict(x) for x in before["v"]]}
        after["v"].insert(30, {"id": 999, "ok": True})
        after["v"][-1]["ok"] = False
        result = pd.diff(before, after)
        self.assertEqual([(c.path, c.op) for c in result.changes],
                         [("$.v[30]", "add"), ("$.v[60].ok", "change")])

    def test_shifted_large_array_has_no_cascade(self):
        before = {"v": [{"id": i, "note": "same"} for i in range(300)]}
        after = {"v": [dict(x) for x in before["v"]]}
        del after["v"][50]
        after["v"].insert(200, {"id": 999, "note": "new"})
        after["v"][120]["note"] = "edited"
        result = pd.diff(before, after)
        self.assertLessEqual(len(result.changes), 4)
        self.assertFalse(any(c.path.startswith("$.v[5") and c.op == "change"
                             and c.path.endswith("]") for c in result.changes))

    def test_natural_index_sort(self):
        before = {"v": list(range(12))}
        after = {"v": list(before["v"])}
        after["v"][2] = 102
        after["v"][10] = 110
        got = ops(before, after)
        self.assertEqual([p for p, _ in got], ["$.v[2]", "$.v[10]"])


class Ignore(unittest.TestCase):
    def test_ignore_exact_path(self):
        self.assertEqual(ops({"a": 1, "b": 2}, {"a": 9, "b": 9},
                            ["$.a"]), [("$.b", "change")])

    def test_ignore_subtree(self):
        before = {"meta": {"ts": 1, "host": "x"}, "port": 1}
        after = {"meta": {"ts": 2, "host": "x"}, "port": 2}
        self.assertEqual(ops(before, after, ["$.meta"]),
                         [("$.port", "change")])

    def test_ignore_wildcard_index(self):
        before = {"items": [{"id": 1, "ts": 1}, {"id": 2, "ts": 2}]}
        after = {"items": [{"id": 1, "ts": 8}, {"id": 2, "ts": 9}]}
        self.assertEqual(ops(before, after, ["$.items[*].ts"]), [])

    def test_ignore_wildcard_covers_inserted_elements(self):
        before = {"features": ["a", "b"]}
        after = {"features": ["a", "x", "b"], "port": 1}
        self.assertEqual(ops(before, after, ["$.features[*]"]),
                         [("$.port", "add")])

    def test_ignore_multiple_patterns(self):
        self.assertEqual(ops({"a": 1, "b": 1, "c": 1},
                             {"a": 2, "b": 2, "c": 2},
                             ["$.a", "$.b"]), [("$.c", "change")])

    def test_ignore_root_skips_everything(self):
        self.assertEqual(changes({"a": 1}, {"b": 2}, ["$"]), [])

    def test_ignore_does_not_leak_into_sibling(self):
        self.assertEqual(ops({"alpha": 1, "alphabet": 1},
                             {"alpha": 2, "alphabet": 2}, ["$.alpha"]),
                         [("$.alphabet", "change")])


class Api(unittest.TestCase):
    def test_result_has_stats(self):
        result = pd.diff({"v": [1, 2, 3]}, {"v": [1, 2, 3, 4]})
        self.assertEqual(result.arrays, 1)
        self.assertEqual(result.matched, 3)
        self.assertTrue(result.notes)

    def test_notes_describe_alignment(self):
        result = pd.diff({"v": [1, 2, 3]}, {"v": [1, 99, 3]})
        self.assertTrue(any("align $.v" in note for note in result.notes))

    def test_matched_counter_on_identical_array(self):
        self.assertEqual(pd.diff({"v": [1, 2]}, {"v": [1, 2]}).matched, 2)

    def test_why_nonempty_for_every_change_type(self):
        result = pd.diff({"a": 1, "b": [1]}, {"a": 2, "b": [1, 2], "c": 3})
        self.assertTrue(all(c.why for c in result.changes))
        self.assertEqual({c.op for c in result.changes},
                         {"add", "change"})


class Decoding(unittest.TestCase):
    def test_duplicate_keys_are_recorded(self):
        value = pd.decode('{"a": 1, "a": 2}', "x")
        self.assertEqual(value["a"], 2)
        self.assertEqual(pd.duplicate_paths(value), [("$.a", 1)])

    def test_nonstandard_number_is_rejected(self):
        with self.assertRaises(ValueError):
            pd.decode('{"a": NaN}', "x")

    def test_bom_is_stripped(self):
        value = pd.decode("\ufeff{\"a\": 1}", "x")
        self.assertEqual(value, {"a": 1})

    def test_marked_dict_fingerprint_matches_plain_dict(self):
        self.assertEqual(pd.fingerprint(pd.decode('{"a":1}', "x")),
                         pd.fingerprint({"a": 1}))


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, value):
        path = self.dir / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def test_default_output_and_exit_1(self):
        a = self.write("a.json", {"port": 8080})
        b = self.write("b.json", {"port": 8443})
        proc = run_cli(a, b)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("~ $.port: 8080 -> 8443", proc.stdout)
        self.assertIn("1 change(s)", proc.stdout)

    def test_identical_exit_0(self):
        a = self.write("a.json", {"x": 1})
        proc = run_cli(a, a)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("0 change(s)", proc.stdout)

    def test_json_output_is_parseable(self):
        a = self.write("a.json", {"v": [1, 2]})
        b = self.write("b.json", {"v": [1, 2, 3]})
        proc = run_cli("--json", a, b)
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload[0]["path"], "$.v[2]")
        self.assertEqual(payload[0]["op"], "add")

    def test_json_on_identical_is_empty_array(self):
        a = self.write("a.json", {"x": 1})
        proc = run_cli("--json", a, a)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout), [])

    def test_explain_adds_reasons_notes_and_why_field(self):
        a = self.write("a.json", {"v": [1, 2]})
        b = self.write("b.json", {"v": [1, 2, 3]})
        proc = run_cli("--explain", a, b)
        self.assertIn("why:", proc.stdout)
        self.assertIn("# alignment notes", proc.stdout)
        self.assertIn("align $.v", proc.stdout)
        proc2 = run_cli("--json", "--explain", a, b)
        self.assertIn("why", json.loads(proc2.stdout)[0])

    def test_stdin_as_after(self):
        a = self.write("a.json", {"x": 1})
        proc = run_cli(a, "-", stdin='{"x": 2}')
        self.assertEqual(proc.returncode, 1)
        self.assertIn("~ $.x: 1 -> 2", proc.stdout)

    def test_stdin_as_before(self):
        b = self.write("b.json", {"x": 1})
        proc = run_cli("-", b, stdin='{"x": 2}')
        self.assertEqual(proc.returncode, 1)
        self.assertIn("~ $.x: 2 -> 1", proc.stdout)

    def test_bad_json_exit_2(self):
        bad = self.dir / "bad.json"
        bad.write_text("{oops", encoding="utf-8")
        good = self.write("a.json", {"x": 1})
        proc = run_cli(str(bad), good)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("error:", proc.stderr)

    def test_missing_file_exit_2(self):
        proc = run_cli(str(self.dir / "nope.json"), str(self.dir / "nope2.json"))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("error:", proc.stderr)

    def test_bad_ignore_exit_2(self):
        a = self.write("a.json", {"x": 1})
        proc = run_cli("--ignore", "x", a, a)
        self.assertEqual(proc.returncode, 2)

    def test_cli_ignore(self):
        a = self.write("a.json", {"a": 1, "b": 1})
        b = self.write("b.json", {"a": 2, "b": 2})
        proc = run_cli("--ignore", "$.a", a, b)
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("$.a", proc.stdout)
        self.assertIn("$.b", proc.stdout)

    def test_bom_file_via_cli(self):
        path = self.dir / "bom.json"
        path.write_text("\ufeff{\"x\": 1}", encoding="utf-8")
        other = self.write("o.json", {"x": 2})
        proc = run_cli(str(path), other)
        self.assertEqual(proc.returncode, 1)

    def test_duplicate_key_note(self):
        dup = self.dir / "dup.json"
        dup.write_text('{"x": 1, "x": 2}', encoding="utf-8")
        other = self.write("o.json", {"x": 2})
        proc = run_cli("--explain", str(dup), other)
        self.assertIn("duplicate key", proc.stdout)

    def test_examples_are_shipped_and_usable(self):
        before = ROOT / "examples" / "before.json"
        after = ROOT / "examples" / "after.json"
        self.assertTrue(before.exists() and after.exists())
        proc = run_cli(str(before), str(after))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("+ $.features[2]: \"dark-mode\"", proc.stdout)
        self.assertIn("6 change(s)", proc.stdout)


class Invariants(unittest.TestCase):
    """The printed change list must be enough to rebuild the AFTER document."""

    KEYS = ["alpha", "beta", "gamma", "delta", "a.b", "名称"]

    def random_object(self, rng, depth=0):
        obj = {}
        for key in rng.sample(self.KEYS, rng.randint(1, 5)):
            roll = rng.random()
            if depth < 2 and roll < 0.30:
                obj[key] = self.random_object(rng, depth + 1)
            elif roll < 0.50:
                obj[key] = rng.randint(0, 9)
            elif roll < 0.68:
                obj[key] = rng.choice([True, False, None])
            else:
                obj[key] = rng.choice(["x", "y", "中文", "", "1"])
        return obj

    def parent(self, node, tokens):
        for token in tokens[:-1]:
            node = node[token]
        return node

    def test_change_list_rebuilds_after(self):
        for seed in range(60):
            rng = random.Random(seed)
            before = self.random_object(rng)
            after = self.random_object(rng)
            result = pd.diff(before, after)
            rebuilt = copy.deepcopy(before)
            for change in result.changes:
                holder = self.parent(rebuilt, change.tokens)
                if change.op == "remove":
                    del holder[change.tokens[-1]]
                else:
                    holder[change.tokens[-1]] = change.after
            self.assertEqual(rebuilt, after, "seed %d" % seed)

    def test_reported_before_values_live_at_reported_paths(self):
        for seed in range(60):
            rng = random.Random(seed)
            before = self.random_object(rng)
            after = self.random_object(rng)
            for change in pd.diff(before, after).changes:
                if change.op == "add":
                    continue
                node = before
                for token in change.tokens:
                    node = node[token]
                self.assertEqual(pd.fingerprint(node),
                                 pd.fingerprint(change.before))


if __name__ == "__main__":
    unittest.main()
