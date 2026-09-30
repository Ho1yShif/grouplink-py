"""load_config reads only the env it is handed, so each case passes its own."""

from __future__ import annotations

import pytest

from grouplink.config import env_int, load_config

ENV = {
    "NOTION_LINKS_DATABASE_ID": "db_links",
    "NOTION_PROFILES_DATABASE_ID": "db_profiles",
    "SITE_DEFAULT_SLUG": "shifra",
}


def load(**extra: str):
    return load_config({}, {**ENV, **extra})


class TestLoadConfig:
    def test_defaults_dry_run_to_false(self) -> None:
        assert load().dry_run is False

    @pytest.mark.parametrize("value", ["false", "False", "FALSE", " off ", "no", "0"])
    def test_reads_a_falsy_dry_run_whatever_the_casing(self, value: str) -> None:
        assert load(DRY_RUN=value).dry_run is False

    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes"])
    def test_reads_a_truthy_dry_run_whatever_the_casing(self, value: str) -> None:
        assert load(DRY_RUN=value).dry_run is True

    @pytest.mark.parametrize("value", ["", "   "])
    def test_falls_back_when_dry_run_is_empty_or_whitespace(self, value: str) -> None:
        assert load(DRY_RUN=value).dry_run is False

    def test_prefers_the_run_input_over_the_env(self) -> None:
        cfg = load_config({"dryRun": False}, {**ENV, "DRY_RUN": "true"})
        assert cfg.dry_run is False

    def test_lowercases_and_trims_the_default_slug(self) -> None:
        assert load(SITE_DEFAULT_SLUG=" Shifra ").default_slug == "shifra"

    def test_reads_the_limit_from_the_env(self) -> None:
        assert load(LINKS_LIMIT="25").limit == 25

    def test_rejects_a_non_numeric_limit(self) -> None:
        with pytest.raises(ValueError, match="LINKS_LIMIT"):
            load(LINKS_LIMIT="lots")

    def test_names_the_variable_that_is_missing(self) -> None:
        with pytest.raises(ValueError, match="NOTION_LINKS_DATABASE_ID"):
            load_config({}, {})
        with pytest.raises(ValueError, match="NOTION_PROFILES_DATABASE_ID"):
            load_config({}, {"NOTION_LINKS_DATABASE_ID": "x"})
        with pytest.raises(ValueError, match="SITE_DEFAULT_SLUG"):
            load_config({}, {**ENV, "SITE_DEFAULT_SLUG": ""})


class TestEnvInt:
    @pytest.mark.parametrize("value", [None, "", "  "])
    def test_falls_back_when_the_variable_is_unset_empty_or_whitespace(
        self, value: str | None
    ) -> None:
        assert env_int("LINKS_LIMIT", value, 100) == 100

    @pytest.mark.parametrize("value", ["25", " 25 "])
    def test_reads_a_whole_number_with_or_without_surrounding_space(self, value: str) -> None:
        assert env_int("LINKS_LIMIT", value, 100) == 25

    @pytest.mark.parametrize("value", ["-5", "0", "10abc", "1e3", "2.5", "+5", "abc", "١٢"])
    def test_rejects_anything_that_is_not_a_whole_number_of_1_or_more(self, value: str) -> None:
        with pytest.raises(ValueError, match="LINKS_LIMIT"):
            env_int("LINKS_LIMIT", value, 100)
