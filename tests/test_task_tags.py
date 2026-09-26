"""The task-tag vocabulary and the leading-tag split, pinned.

The behaviours guarded here are the ones that have broken before: a tag matched in the
middle of a title, a known-looking token that is not actually known, and a tag whose
spelling changed on the way back out. Each would silently corrupt a human's title, so
each is asserted directly rather than through the parser that uses them.

The legacy ``UI`` alias is pinned here too. The plan on disk carries ``[UI]`` tokens
written under the old vocabulary, so ``split_tag`` must keep recognising them and hand
back the canonical ``FE`` -- dropping the alias would leave ``[UI]`` in the title as
literal text and compound on every save.
"""

import unittest

from tools.task_tags import (
    ALIASES,
    CANONICAL,
    CATEGORIES,
    LABELS,
    ORDER,
    TAGS,
    UI_TAG,
    category_of,
    is_tag,
    split_tag,
    tag_prefix,
)


class VocabularyTests(unittest.TestCase):
    def test_every_tag_appears_exactly_once(self):
        self.assertEqual(len(ORDER), len(set(ORDER)))

    def test_the_three_indexes_agree_with_the_order(self):
        self.assertEqual(list(TAGS), ORDER)
        self.assertEqual(list(LABELS), ORDER)
        self.assertEqual(list(CATEGORIES), list(LABELS.values()))

    def test_no_tag_breaks_the_bracket_form(self):
        for tag in ORDER:
            self.assertTrue(tag, tag)
            self.assertNotIn("[", tag)
            self.assertNotIn("]", tag)
            self.assertNotIn(" ", tag)

    def test_tags_and_canonical_agree(self):
        for tag in ORDER:
            self.assertEqual(CANONICAL[tag.upper()], tag)

    def test_ui_tag_names_a_real_tag(self):
        self.assertIn(UI_TAG, TAGS)


class LegacyAliasTests(unittest.TestCase):
    def test_ui_is_a_recognised_spelling(self):
        self.assertTrue(is_tag("UI"))
        self.assertTrue(is_tag("ui"))

    def test_ui_is_not_a_tag_of_its_own(self):
        self.assertNotIn("UI", TAGS)
        self.assertNotIn("UI", ORDER)

    def test_ui_canonicalises_to_fe(self):
        self.assertEqual(ALIASES["UI"], "FE")
        self.assertEqual(CANONICAL["UI"], "FE")
        self.assertEqual(UI_TAG, "FE")


class IsTagTests(unittest.TestCase):
    def test_accepts_the_canonical_spelling(self):
        self.assertTrue(is_tag("FE"))
        self.assertTrue(is_tag("BUG"))

    def test_is_case_insensitive(self):
        self.assertTrue(is_tag("fe"))
        self.assertTrue(is_tag("bug"))

    def test_rejects_an_unknown_token(self):
        self.assertFalse(is_tag("WIP"))
        self.assertFalse(is_tag("NOPE"))


class CategoryOfTests(unittest.TestCase):
    def test_returns_the_label(self):
        self.assertEqual(category_of("FE"), LABELS["FE"])

    def test_a_legacy_alias_answers_with_its_canonical_label(self):
        self.assertEqual(category_of("UI"), LABELS["FE"])

    def test_is_case_insensitive(self):
        self.assertEqual(category_of("fe"), LABELS["FE"])

    def test_returns_none_for_an_unknown_tag(self):
        self.assertIsNone(category_of("WIP"))


class TagPrefixTests(unittest.TestCase):
    def test_prefixes_canonically_with_a_trailing_space(self):
        self.assertEqual(tag_prefix("FE"), "[FE] ")

    def test_lower_case_input_is_canonicalised(self):
        self.assertEqual(tag_prefix("fe"), "[FE] ")


class SplitTagTests(unittest.TestCase):
    def test_splits_a_leading_tag(self):
        self.assertEqual(split_tag("[FE] Dashboard view"), ("Dashboard view", "FE"))

    def test_lower_case_input_returns_the_canonical_spelling(self):
        self.assertEqual(split_tag("[fe] Dashboard view"), ("Dashboard view", "FE"))

    def test_a_title_that_is_only_the_tag(self):
        self.assertEqual(split_tag("[BUG]"), ("", "BUG"))

    def test_a_tag_that_is_not_at_the_start_is_left_alone(self):
        self.assertEqual(split_tag("Fix the [API] layer"), ("Fix the [API] layer", None))

    def test_an_unknown_bracketed_token_is_left_alone(self):
        self.assertEqual(split_tag("[WIP] something"), ("[WIP] something", None))

    def test_a_tag_with_a_hyphen_is_plain_prose(self):
        # The new vocabulary has no hyphenated tags; the token must stay in the title.
        self.assertEqual(split_tag("[API-DOCS] update"), ("[API-DOCS] update", None))

    def test_surrounding_whitespace_is_stripped_from_the_remainder(self):
        self.assertEqual(split_tag("[FE]    Dashboard view   "), ("Dashboard view", "FE"))

    def test_a_tag_inside_backticks_is_prose(self):
        self.assertEqual(split_tag("Wire `[FE]` tagging"), ("Wire `[FE]` tagging", None))

    def test_a_tag_with_no_title_at_all(self):
        self.assertEqual(split_tag("[FE]"), ("", "FE"))


class LegacyUiAliasTests(unittest.TestCase):
    """The back-compat contract: an on-disk ``[UI]`` still parses, as ``FE``."""

    def test_a_legacy_ui_prefix_is_recognised_and_canonicalised(self):
        self.assertEqual(split_tag("[UI] legacy title"), ("legacy title", "FE"))

    def test_a_lower_case_legacy_ui_prefix_is_canonicalised(self):
        self.assertEqual(split_tag("[ui] legacy title"), ("legacy title", "FE"))

    def test_a_legacy_ui_prefix_lower_case_round_trips_as_fe(self):
        self.assertEqual(split_tag("[ui] Some title"), ("Some title", "FE"))


class RoundTripTests(unittest.TestCase):
    def test_every_tag_survives_a_compile_and_split(self):
        for tag in ORDER:
            title = tag_prefix(tag) + "Some title"
            self.assertEqual(split_tag(title), ("Some title", tag))


if __name__ == "__main__":
    unittest.main()
