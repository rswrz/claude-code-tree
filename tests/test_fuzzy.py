"""Query parsing and matching.

Expectations are cross-checked against ``fzf --filter <q> --exact``.
"""

from __future__ import annotations

import pytest

from cct.fuzzy import Query, TermType, matches, parse, spans, toggle_token


def m(query: str, text: str) -> bool:
    return matches(parse(query), text)


def types(query: str) -> list[tuple[TermType, bool, str]]:
    return [(t.typ, t.inv, t.text) for t in parse(query).terms]


class TestParse:
    def test_empty_query_has_no_terms(self):
        assert parse("").sets == ()
        assert not parse("   ")

    def test_bare_term_is_exact_in_exact_mode(self):
        assert types("deploy") == [(TermType.EXACT, False, "deploy")]

    def test_and_terms_become_separate_groups(self):
        q = parse("deploy prodtest")
        assert len(q.sets) == 2
        assert all(len(g) == 1 for g in q.sets)

    def test_bar_ors_within_one_group(self):
        q = parse("a | b | c")
        assert len(q.sets) == 1
        assert [t.text for t in q.sets[0]] == ["a", "b", "c"]

    def test_leading_bar_starts_a_term(self):
        assert types("| a") == [(TermType.EXACT, False, "|"), (TermType.EXACT, False, "a")]

    def test_anchors(self):
        assert types("^main") == [(TermType.PREFIX, False, "main")]
        assert types("main$") == [(TermType.SUFFIX, False, "main")]
        assert types("^main$") == [(TermType.EQUAL, False, "main")]

    def test_quote_flips_to_fuzzy(self):
        assert types("'abc") == [(TermType.FUZZY, False, "abc")]

    def test_quote_wins_over_a_suffix_anchor(self):
        assert types("'abc$") == [(TermType.FUZZY, False, "abc")]

    def test_negation_combines_with_anchors(self):
        assert types("!abc") == [(TermType.EXACT, True, "abc")]
        assert types("!^abc") == [(TermType.PREFIX, True, "abc")]
        assert types("!abc$") == [(TermType.SUFFIX, True, "abc")]

    def test_lone_sigils_are_dropped(self):
        assert parse("'").sets == ()
        assert parse("!").sets == ()
        assert parse("^").sets == ()

    def test_lone_dollar_stays_a_literal(self):
        assert types("$") == [(TermType.EXACT, False, "$")]

    def test_smart_case(self):
        assert parse("deploy").terms[0].case_sensitive is False
        assert parse("Deploy").terms[0].case_sensitive is True
        # the sigil counts as part of the token fzf case-checks
        assert parse("!Deploy").terms[0].case_sensitive is True

    def test_no_break_space_stays_inside_one_term(self):
        q = parse("⎇\xa0main bg:")
        assert [t.text for t in q.terms] == ["⎇\xa0main", "bg:"]

    def test_escaped_space_stays_inside_one_term(self):
        assert types("a\\ b") == [(TermType.EXACT, False, "a b")]

    def test_raw_query_is_kept(self):
        assert parse("a b").raw == "a b"


class TestMatches:
    def test_empty_query_matches_everything(self):
        assert m("", "anything")

    def test_substring(self):
        assert m("ploy", "deploy prodtest")
        assert not m("plot", "deploy prodtest")

    def test_and(self):
        assert m("deploy prodtest", "deploy prodtest")
        assert not m("deploy missing", "deploy prodtest")

    def test_or(self):
        assert m("deploy | missing", "deploy prodtest")
        assert not m("nope | missing", "deploy prodtest")

    def test_negation(self):
        assert m("!missing", "deploy")
        assert not m("!deploy", "deploy")

    def test_negation_inside_an_or_group(self):
        # "not abc, or else xyz"
        assert m("!abc | xyz", "xyz here")
        assert m("!abc | xyz", "nothing")
        assert not m("!abc | xyz", "abc only")

    def test_prefix_skips_leading_whitespace(self):
        assert m("^abc", "   abc de")
        assert not m("^abc", "x abc")

    def test_prefix_with_a_space_pattern_does_not_skip(self):
        assert m("^\\ abc", " abc")

    def test_suffix_skips_trailing_whitespace(self):
        assert m("abc$", "de abc   ")
        assert not m("abc$", "abc de")

    def test_equal_ignores_surrounding_whitespace(self):
        assert m("^abc$", "  abc  ")
        assert not m("^abc$", "abcd")

    def test_smart_case_insensitive(self):
        assert m("deploy", "DEPLOY")

    def test_smart_case_sensitive(self):
        assert not m("Deploy", "deploy")
        assert m("Deploy", "Deploy")

    def test_fuzzy_subsequence(self):
        assert m("'dpl", "deploy")
        assert not m("'dpz", "deploy")

    def test_fuzzy_is_case_smart_too(self):
        assert m("'qbf", "The Quick Brown Fox")
        assert not m("'QBF", "the quick brown fox")


class TestSpans:
    def test_all_occurrences_of_an_exact_term(self):
        assert spans(parse("ab"), "ab cd ab") == [(0, 2), (6, 8)]

    def test_case_insensitive_hits(self):
        assert spans(parse("ab"), "AB") == [(0, 2)]

    def test_negated_terms_never_highlight(self):
        assert spans(parse("!ab"), "ab cd") == []

    def test_terms_from_every_group(self):
        assert spans(parse("ab cd"), "ab cd") == [(0, 2), (3, 5)]

    def test_overlapping_hits_merge(self):
        assert spans(parse("ab bc"), "abc") == [(0, 3)]

    def test_anchored_terms(self):
        assert spans(parse("^ab"), "  abx") == [(2, 4)]
        assert spans(parse("bx$"), "abx  ") == [(1, 3)]

    def test_fuzzy_marks_the_matched_characters(self):
        assert spans(parse("'dpl"), "deploy") == [(0, 1), (2, 4)]

    def test_no_match_yields_nothing(self):
        assert spans(parse("zz"), "abc") == []


class TestToggleToken:
    def test_adds_a_missing_token(self):
        assert toggle_token("", "bg:") == "bg:"
        assert toggle_token("adr", "bg:") == "adr bg:"

    def test_removes_a_present_token(self):
        assert toggle_token("adr bg:", "bg:") == "adr"
        assert toggle_token("bg:", "bg:") == ""

    def test_splits_on_the_plain_space_only(self):
        # the branch token carries a no-break space and must survive as one field
        tok = "⎇\xa0feat/x"
        assert toggle_token("adr", tok) == f"adr {tok}"
        assert toggle_token(f"adr {tok}", tok) == "adr"

    def test_collapses_runs_of_spaces(self):
        assert toggle_token("a   b", "c") == "a b c"

    def test_empty_token_is_a_no_op_beyond_normalising(self):
        assert toggle_token("a  b", "") == "a b"

    def test_matches_whole_fields_only(self):
        assert toggle_token("bg:done", "bg:") == "bg:done bg:"


def test_query_is_hashable_and_reusable():
    q = parse("a | b")
    assert isinstance(q, Query)
    assert hash(q) == hash(parse("a | b"))


@pytest.mark.parametrize("query,text,expected", [
    ("ACME", "ACME-316 ADR", True),
    ("acme", "ACME-316 ADR", True),
    ("ACME-316 !done", "ACME-316 ADR", True),
    ("ACME-316 !ADR", "ACME-316 ADR", False),
    ("bg:", "review  bg:done", True),
    ("^bg:", "review  bg:done", False),
])
def test_realistic_queries(query, text, expected):
    assert m(query, text) is expected
