"""The task-tag vocabulary and the leading-tag split, pinned.

The behaviours guarded here are the ones that have broken before: a tag matched in the
middle of a title, a known-looking token that is not actually known, and a tag whose
spelling changed on the way back out. Each would silently corrupt a human's title, so
each is asserted directly rather than through the parser that uses them.
"""

import unittest

from tools.task_tags import (
    CANONICAL,
    CATEGORIES,
    TAGS,
    UI_TAG,
    VOCABULARY,
    category_of,
    is_tag,
    split_tag,
    tag_prefix,
)


class VocabularyTests(unittest.TestCase):
    def test_categories_track_the_vocabulary_order(self):
        self.assertEqual(CATEGORIES, list(VOCABULARY))

    def test_tags_and_canonical_agree(self):
        for tag in TAGS:
            self.assertEqual(CANONICAL[tag.upper()], tag)

    def test_every_tag_appears_exactly_once(self):
        seen = [tag for entries in VOCABULARY.values() for tag, _ in entries]
        self.assertEqual(len(seen), len(set(seen)))

    def test_no_tag_breaks_the_bracket_form(self):
        for tag in TAGS:
            self.assertNotIn("[", tag)
            self.assertNotIn("]", tag)
            self.assertNotIn(" ", tag)

    def test_ui_tag_names_a_real_tag(self):
        self.assertIn(UI_TAG, TAGS)


class IsTagTests(unittest.TestCase):
    def test_accepts_the_canonical_spelling(self):
        self.assertTrue(is_tag("UI"))
        self.assertTrue(is_tag("BUG"))

    def test_is_case_insensitive(self):
        self.assertTrue(is_tag("ui"))
        self.assertTrue(is_tag("bug"))

    def test_rejects_an_unknown_token(self):
        self.assertFalse(is_tag("WIP"))
        self.assertFalse(is_tag("NOPE"))


class CategoryOfTests(unittest.TestCase):
    def test_returns_the_category(self):
        self.assertEqual(category_of("UI"), "Frontend & User Experience")

    def test_is_case_insensitive(self):
        self.assertEqual(category_of("ui"), "Frontend & User Experience")

    def test_returns_none_for_an_unknown_tag(self):
        self.assertIsNone(category_of("WIP"))


class TagPrefixTests(unittest.TestCase):
    def test_prefixes_canonically_with_a_trailing_space(self):
        self.assertEqual(tag_prefix("UI"), "[UI] ")

    def test_lower_case_input_is_canonicalised(self):
        self.assertEqual(tag_prefix("ui"), "[UI] ")


class SplitTagTests(unittest.TestCase):
    def test_splits_a_leading_tag(self):
        self.assertEqual(split_tag("[UI] Dashboard view"), ("Dashboard view", "UI"))

    def test_lower_case_input_returns_the_canonical_spelling(self):
        self.assertEqual(split_tag("[ui] Dashboard view"), ("Dashboard view", "UI"))

    def test_a_title_that_is_only_the_tag(self):
        self.assertEqual(split_tag("[BUG]"), ("", "BUG"))

    def test_a_tag_that_is_not_at_the_start_is_left_alone(self):
        self.assertEqual(split_tag("Fix the [API] layer"), ("Fix the [API] layer", None))

    def test_an_unknown_bracketed_token_is_left_alone(self):
        self.assertEqual(split_tag("[WIP] something"), ("[WIP] something", None))

    def test_a_tag_with_a_slash(self):
        self.assertEqual(split_tag("[CI/CD] pipeline"), ("pipeline", "CI/CD"))

    def test_a_tag_with_a_hyphen(self):
        self.assertEqual(split_tag("[API-DOCS] update"), ("update", "API-DOCS"))

    def test_surrounding_whitespace_is_stripped_from_the_remainder(self):
        self.assertEqual(split_tag("[UI]    Dashboard view   "), ("Dashboard view", "UI"))

    def test_a_tag_inside_backticks_is_prose(self):
        self.assertEqual(split_tag("Wire `[UI]` tagging"), ("Wire `[UI]` tagging", None))

    def test_a_tag_with_no_title_at_all(self):
        self.assertEqual(split_tag("[UI]"), ("", "UI"))


class RoundTripTests(unittest.TestCase):
    def test_every_tag_survives_a_compile_and_split(self):
        for tag in TAGS:
            title = tag_prefix(tag) + "Some title"
            self.assertEqual(split_tag(title), ("Some title", tag))


if __name__ == "__main__":
    unittest.main()
