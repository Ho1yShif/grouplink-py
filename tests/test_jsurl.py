from __future__ import annotations

import pytest

from grouplink.jsurl import normalize_url


class TestNormalizeUrl:
    @pytest.mark.parametrize(
        ("cell", "expected"),
        [
            ("http://render.com/", "https://render.com/"),
            ("HTTP://render.com/", "https://render.com/"),
            ("https://render.com/", "https://render.com/"),
            ("HTTPS://render.com/", "https://render.com/"),
            ("render.com", "https://render.com"),
            ("render.com/careers", "https://render.com/careers"),
            ("render.com:8080/x", "https://render.com:8080/x"),
            ("  render.com  ", "https://render.com"),
            ("ren\tder.com", "https://render.com"),
            ("mailto:shifra@render.com", "mailto:shifra@render.com"),
            ("MAILTO:shifra@render.com", "mailto:shifra@render.com"),
            ("mailto:", "mailto:"),
            ("ftp://example.com/f", "ftp://example.com/f"),
            ("javascript:alert(1)", "javascript:alert(1)"),
            ("", ""),
            ("   ", ""),
        ],
    )
    def test_normalizes_a_notion_cell(self, cell: str, expected: str) -> None:
        assert normalize_url(cell) == expected

    def test_upgrades_http_without_touching_the_rest_of_the_url(self) -> None:
        cell = "http://user@render.com:8080/a/b?q=1#f"
        assert normalize_url(cell) == "https://user@render.com:8080/a/b?q=1#f"

    def test_is_idempotent(self) -> None:
        for cell in ["render.com", "http://render.com", "mailto:a@b.com", "ftp://x.com"]:
            once = normalize_url(cell)
            assert normalize_url(once) == once
