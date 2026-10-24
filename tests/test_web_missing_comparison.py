"""The arithmetic of the missing-page check: what makes two documents of one host the same page, and which pages
are worth asking about (``web_scraper/web_tools/missing.py``; the check itself is in ``test_web_missing_pages.py``)."""

from __future__ import annotations

import pytest

from web_scraper.web_tools.missing import (
    SAME_WORDS,
    fingerprint,
    probe_url,
    same_page,
    suspect,
    title_key,
)

WELCOME = "Acme Studio designs, builds and maintains web and mobile software for small businesses. "


class TestTheComparison:
    def test_the_title_and_the_text_must_both_be_the_same(self) -> None:
        home = fingerprint("Acme — Home", WELCOME * 4)

        assert same_page(
            home, fingerprint("acme  —  HOME", WELCOME * 4)
        )  # spacing and case are not differences
        assert not same_page(
            home, fingerprint("About | Acme", WELCOME * 4)
        )  # another title: a page of its own
        assert not same_page(
            home, fingerprint("Acme — Home", "A different text altogether, about other things.")
        )

    def test_nine_words_in_ten_alike_is_the_same_text(self) -> None:
        words = [f"word{n}" for n in range(100)]
        left = fingerprint("T", " ".join(words))
        nearly = fingerprint("T", " ".join(words[:95] + ["x1", "x2", "x3", "x4"]))
        less = fingerprint("T", " ".join(words[:80] + [f"other{n}" for n in range(20)]))

        assert same_page(left, nearly) and not same_page(left, less)
        assert SAME_WORDS == 0.9

    def test_a_fallback_that_names_the_address_it_was_asked_for_is_still_the_fallback(self) -> None:
        body = WELCOME * 6
        left = fingerprint(
            "Not found", body + "Nothing at /bu-sayfa-yok-12345.", "/bu-sayfa-yok-12345"
        )
        right = fingerprint(
            "Not found",
            body + "Nothing at /web-research-probe-9f3a6c1e2b7d4a58.",
            "/web-research-probe-9f3a6c1e2b7d4a58",
        )

        assert same_page(left, right)
        # Without the words of the addresses left out the two texts would differ by eight words in twenty.
        assert not same_page(
            fingerprint("Not found", body + "Nothing at /bu-sayfa-yok-12345."),
            fingerprint("Not found", body + "Nothing at /web-research-probe-9f3a6c1e2b7d4a58."),
        )
        assert not same_page(
            left, fingerprint("Not found", "Something else entirely, said briefly.")
        )

    def test_the_text_is_compared_from_its_start(self) -> None:
        start = "alpha beta gamma delta " * 400  # 8,800 characters: more than is compared
        assert same_page(fingerprint("T", start + "one"), fingerprint("T", start + "two"))

    @pytest.mark.parametrize(
        ("final_url", "declared", "title", "root", "expected"),
        [
            ("https://x.test/", "", "T", "", False),  # the root is what a fallback shows
            ("https://x.test/p", "", "T", "", True),  # names no canonical
            ("https://x.test/p", "https://x.test/", "T", "", True),  # names another address
            ("https://x.test/p", "https://x.test/p", "T", "", False),  # names itself
            (
                "https://x.test/p/",
                "https://x.test/p?utm_source=a",
                "T",
                "",
                False,
            ),  # the same page, spelt otherwise
            (
                "https://x.test/p",
                "https://x.test/p",
                "Home",
                title_key("Home"),
                True,
            ),  # titled as the root
            ("https://x.test/p", "https://x.test/p", "Other", title_key("Home"), False),
            ("https://x.test/p", "not a url", "T", "", True),
        ],
    )
    def test_which_pages_may_be_a_fallback(
        self, final_url: str, declared: str, title: str, root: str, expected: bool
    ) -> None:
        assert suspect(final_url, declared, title, root) is expected


def test_the_probe_path_is_a_random_one_of_the_same_host() -> None:
    first, second = probe_url("https://soft.test"), probe_url("https://soft.test/")

    assert first != second
    assert first.startswith("https://soft.test/web-research-probe-")
    assert second.startswith("https://soft.test/web-research-probe-")
