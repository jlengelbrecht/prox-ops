"""Positive projection helpers: exact types, bounded failures, no pass-through."""

import unittest

import _support as S  # noqa: F401 - puts the package on sys.path
from orca_adapter import errors
from orca_adapter.fields import MAX_STR_CHARS, is_wellformed_text, take_bool, take_dict, take_int, take_rows, take_str, take_str_list


class FieldTests(unittest.TestCase):
    def _malformed(self, fn, obj, key):
        with self.assertRaises(errors.AdapterError, msg=(fn.__name__, obj)) as cm:
            fn(obj, key, "stage")
        self.assertEqual((cm.exception.code, cm.exception.stage, cm.exception.detail),
                         (errors.MALFORMED_RESULT, "stage", key))
        self.assertNotIn("CANARY", repr(cm.exception.to_json()))

    def test_str(self):
        self.assertEqual(take_str({"k": "v"}, "k", "stage"), "v")
        self.assertIsNone(take_str({"k": None}, "k", "stage", optional=True))  # explicit null
        for obj in ({}, {"k": ""}, {"k": 1}, {"k": ["CANARY"]}, {"k": None}, {"k": "a\ud800b"}, {"k": "x" * (MAX_STR_CHARS + 1)}):
            self._malformed(take_str, obj, "k")
        optional = lambda o, k, s: take_str(o, k, s, optional=True)  # noqa: E731
        self._malformed(optional, {}, "k")  # an absent key is never an explicit null
        self._malformed(optional, {"k": ""}, "k")
        self._malformed(optional, {"k": "\udfff"}, "k")

    def test_wellformed_text(self):
        self.assertTrue(is_wellformed_text("run_aaa"))
        self.assertTrue(is_wellformed_text("h\u00e9llo \U0001f600"))  # any real Unicode text is fine
        for bad in ("\ud800", "\udcff", "a\udbffz", 1, None, b"x", "x" * (MAX_STR_CHARS + 1)):
            self.assertFalse(is_wellformed_text(bad), repr(bad))

    def test_bool_and_int_are_type_exact(self):
        self.assertIs(take_bool({"k": True}, "k", "stage"), True)
        for obj in ({}, {"k": 1}, {"k": "true"}, {"k": None}):
            self._malformed(take_bool, obj, "k")
        self.assertEqual(take_int({"k": 0}, "k", "stage"), 0)
        for obj in ({}, {"k": True}, {"k": -1}, {"k": 1.0}, {"k": "1"}):
            self._malformed(take_int, obj, "k")

    def test_containers(self):
        self.assertEqual(take_dict({"k": {"a": 1}}, "k", "stage"), {"a": 1})
        self.assertEqual(take_rows({"k": [{"a": 1}]}, "k", "stage"), [{"a": 1}])
        self.assertEqual(take_rows({"k": []}, "k", "stage"), [])
        self.assertEqual(take_str_list({"k": ["a"]}, "k", "stage"), ["a"])
        for obj in ({}, {"k": []}, {"k": "CANARY"}):
            self._malformed(take_dict, obj, "k")
        for obj in ({}, {"k": {}}, {"k": [1]}, {"k": ["CANARY"]}, {"k": "CANARY"}):
            self._malformed(take_rows, obj, "k")
        for obj in ({}, {"k": "CANARY"}, {"k": [1]}, {"k": [None]}, {"k": ["\ud800"]}):
            self._malformed(take_str_list, obj, "k")


if __name__ == "__main__":
    unittest.main()
