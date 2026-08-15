# -*- coding: utf-8 -*-
"""
Tests for coursera/adapter.py

All functions tested here are pure (no network, no filesystem) except
load_conf_file which uses pytest's tmp_path fixture for real file I/O.
"""

import os
import pytest
from types import SimpleNamespace

from coursera.adapter import (
    extract_slug,
    compute_folder,
    slug_to_display,
    build_command,
    load_conf_file,
    make_args,
)


# ---------------------------------------------------------------------------
# extract_slug
# ---------------------------------------------------------------------------

class TestExtractSlug:

    # -- course URLs ----------------------------------------------------------

    def test_learn_url_full(self):
        assert extract_slug("https://www.coursera.org/learn/machine-learning") == \
               ("machine-learning", "course")

    def test_learn_url_no_www(self):
        assert extract_slug("https://coursera.org/learn/ml") == ("ml", "course")

    def test_learn_url_with_query_and_fragment(self):
        assert extract_slug("https://coursera.org/learn/ml?utm=foo#week1") == \
               ("ml", "course")

    def test_learn_url_trailing_slash_stripped(self):
        slug, kind = extract_slug("https://coursera.org/learn/ml/")
        assert slug == "ml"
        assert kind == "course"

    def test_learn_url_with_subpath(self):
        # Anything after the slug (e.g. /home/welcome) should be ignored
        slug, kind = extract_slug("https://www.coursera.org/learn/deep-neural-network/home/welcome")
        assert slug == "deep-neural-network"
        assert kind == "course"

    # -- specialization URLs --------------------------------------------------

    def test_specializations_url(self):
        assert extract_slug("https://www.coursera.org/specializations/deep-learning") == \
               ("deep-learning", "specialization")

    def test_professional_certificates_url(self):
        assert extract_slug(
            "https://www.coursera.org/professional-certificates/ibm-data-science"
        ) == ("ibm-data-science", "specialization")

    def test_professional_certificates_with_query(self):
        slug, kind = extract_slug(
            "https://www.coursera.org/professional-certificates/snowflake-generative-ai"
            "?specialization=snowflake-generative-ai"
        )
        assert slug == "snowflake-generative-ai"
        assert kind == "specialization"

    # -- bare slugs -----------------------------------------------------------

    def test_bare_slug_single_word(self):
        assert extract_slug("machinelearning") == ("machinelearning", "course")

    def test_bare_slug_hyphenated(self):
        assert extract_slug("machine-learning") == ("machine-learning", "course")

    def test_bare_slug_alphanumeric(self):
        assert extract_slug("ml101") == ("ml101", "course")

    # -- invalid inputs -------------------------------------------------------

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError):
            extract_slug("https://example.com/foo")

    def test_uppercase_slug_raises(self):
        # bare-slug regex requires lowercase; uppercase does not match
        with pytest.raises(ValueError):
            extract_slug("Machine-Learning")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            extract_slug("")

    def test_random_text_raises(self):
        with pytest.raises(ValueError):
            extract_slug("not a url at all!")


# ---------------------------------------------------------------------------
# compute_folder
# ---------------------------------------------------------------------------

class TestComputeFolder:

    def test_blank_display_returns_api_slug(self):
        assert compute_folder("ml-course", "") == "ml-course"

    def test_whitespace_only_display_returns_api_slug(self):
        assert compute_folder("ml-course", "   ") == "ml-course"

    def test_display_slugifies_to_same_as_api_slug(self):
        # "Ml Course" → "ml-course" == api_slug → no prefix
        assert compute_folder("ml-course", "Ml Course") == "ml-course"

    def test_normal_rename(self):
        assert compute_folder("ml-course", "Machine Learning") == \
               "machine-learning_ml-course"

    def test_special_chars_stripped(self):
        assert compute_folder("ml", "Machine & Learning!") == \
               "machine-learning_ml"

    def test_leading_trailing_spaces(self):
        assert compute_folder("ml", "  NLP  ") == "nlp_ml"

    def test_multiple_spaces_collapsed(self):
        assert compute_folder("ml", "Natural  Language   Processing") == \
               "natural-language-processing_ml"

    def test_unicode_non_ascii_stripped(self):
        # Non-ASCII chars are stripped by the [^a-z0-9\s-] regex
        result = compute_folder("ml", "Ångström")
        # 'å', 'n', 'g', 's', 't', 'r', 'ö', 'm' → only ASCII letters survive
        assert result.endswith("_ml")

    def test_numbers_in_display_name(self):
        # "Course 101" → slugified "course-101" == api_slug → no prefix
        assert compute_folder("course-101", "Course 101") == "course-101"

    def test_hyphens_in_display_preserved(self):
        result = compute_folder("my-api", "My-API")
        # "my-api" slugifies to "my-api" which equals api_slug → return api_slug
        assert result == "my-api"


# ---------------------------------------------------------------------------
# slug_to_display
# ---------------------------------------------------------------------------

class TestSlugToDisplay:

    def test_hyphenated(self):
        assert slug_to_display("intro-tensorflow") == "Intro Tensorflow"

    def test_single_word(self):
        assert slug_to_display("ml") == "Ml"

    def test_multiple_hyphens(self):
        assert slug_to_display("a-b-c") == "A B C"

    def test_long_slug(self):
        result = slug_to_display("building-generative-ai-apps-to-talk-to-your-data")
        assert result == "Building Generative Ai Apps To Talk To Your Data"

    def test_no_hyphens(self):
        assert slug_to_display("deeplearning") == "Deeplearning"


# ---------------------------------------------------------------------------
# build_command
# ---------------------------------------------------------------------------

class TestBuildCommand:

    def _cmd(self, **kwargs):
        """Call build_command with sensible defaults; override with kwargs."""
        defaults = dict(
            cookies_file=None,
            path=".",
            subtitle_language="en",
            resolution="540p",
            file_formats=["all"],
            jobs=1,
            slugs=["my-course"],
            is_specialization=False,
            overwrite=False,
            download_quizzes=False,
            download_notebooks=False,
        )
        defaults.update(kwargs)
        return build_command(**defaults)

    def test_minimal_contains_base_command(self):
        cmd = self._cmd()
        assert "uv run python" in cmd
        assert "coursera-dl" in cmd

    def test_slug_appears_in_output(self):
        cmd = self._cmd(slugs=["machine-learning"])
        assert "machine-learning" in cmd

    def test_no_specialization_flag_by_default(self):
        cmd = self._cmd(is_specialization=False)
        assert "--specialization" not in cmd

    def test_specialization_flag_single_slug(self):
        cmd = self._cmd(is_specialization=True, slugs=["deep-learning"])
        assert "--specialization" in cmd

    def test_specialization_flag_suppressed_multiple_slugs(self):
        cmd = self._cmd(is_specialization=True, slugs=["course-a", "course-b"])
        assert "--specialization" not in cmd

    def test_default_resolution_suppressed(self):
        cmd = self._cmd(resolution="540p")
        assert "--video-resolution" not in cmd

    def test_non_default_resolution_included(self):
        cmd = self._cmd(resolution="720p")
        assert "--video-resolution" in cmd
        assert "720p" in cmd

    def test_subtitle_language_included(self):
        # "en" is not "all", so it SHOULD appear
        cmd = self._cmd(subtitle_language="en")
        assert "--subtitle-language" in cmd
        assert "en" in cmd

    def test_subtitle_all_suppressed(self):
        cmd = self._cmd(subtitle_language="all")
        assert "--subtitle-language" not in cmd

    def test_jobs_default_suppressed(self):
        cmd = self._cmd(jobs=1)
        assert "--jobs" not in cmd

    def test_jobs_non_default_included(self):
        cmd = self._cmd(jobs=4)
        assert "--jobs" in cmd
        assert "4" in cmd

    def test_file_formats_all_suppressed(self):
        cmd = self._cmd(file_formats=["all"])
        assert " -f " not in cmd

    def test_file_formats_non_default_included(self):
        cmd = self._cmd(file_formats=["mp4", "pdf"])
        assert "-f" in cmd

    def test_overwrite_flag(self):
        assert "--overwrite" not in self._cmd(overwrite=False)
        assert "--overwrite" in self._cmd(overwrite=True)

    def test_download_quizzes_flag(self):
        assert "--download-quizzes" not in self._cmd(download_quizzes=False)
        assert "--download-quizzes" in self._cmd(download_quizzes=True)

    def test_download_notebooks_flag(self):
        assert "--download-notebooks" not in self._cmd(download_notebooks=False)
        assert "--download-notebooks" in self._cmd(download_notebooks=True)

    def test_no_cookies_file_no_c_flag(self):
        cmd = self._cmd(cookies_file=None)
        # When no cookies file, the -c flag should not be present.
        # Check for " -c " (space-bounded) to avoid false match in --subtitle-language etc.
        assert " -c " not in cmd
        assert cmd.strip().startswith("uv run python")

    def test_cookies_file_included(self):
        cmd = self._cmd(cookies_file="./cookies.txt")
        assert "-c" in cmd
        assert "cookies.txt" in cmd

    def test_multiple_slugs_all_present(self):
        cmd = self._cmd(slugs=["course-a", "course-b", "course-c"])
        assert "course-a" in cmd
        assert "course-b" in cmd
        assert "course-c" in cmd

    def test_multiline_format(self):
        # Windows-style continuation: long commands use ^ line continuation
        cmd = self._cmd(
            cookies_file="./cookies.txt",
            resolution="720p",
            subtitle_language="en",
            jobs=2,
            slugs=["ml"],
        )
        assert " ^\n  " in cmd


# ---------------------------------------------------------------------------
# load_conf_file
# ---------------------------------------------------------------------------

class TestLoadConfFile:

    def test_missing_file_returns_empty(self, tmp_path):
        result = load_conf_file(str(tmp_path / "nonexistent.conf"))
        assert result == {}

    def test_empty_file_returns_empty(self, tmp_path):
        f = tmp_path / "coursera-dl.conf"
        f.write_text("")
        assert load_conf_file(str(f)) == {}

    def test_comments_and_blank_lines_ignored(self, tmp_path):
        f = tmp_path / "coursera-dl.conf"
        f.write_text("# this is a comment\n\n   \n")
        assert load_conf_file(str(f)) == {}

    def test_path_parsed(self, tmp_path):
        f = tmp_path / "coursera-dl.conf"
        f.write_text("path = ./courses\n")
        assert load_conf_file(str(f)) == {"path": "./courses"}

    def test_subtitle_language_hyphen_key(self, tmp_path):
        f = tmp_path / "coursera-dl.conf"
        f.write_text("subtitle-language = zh-CN\n")
        assert load_conf_file(str(f)) == {"subtitle_language": "zh-CN"}

    def test_subtitle_language_underscore_alias(self, tmp_path):
        f = tmp_path / "coursera-dl.conf"
        f.write_text("subtitle_language = fr\n")
        assert load_conf_file(str(f)) == {"subtitle_language": "fr"}

    def test_video_resolution_hyphen_key(self, tmp_path):
        f = tmp_path / "coursera-dl.conf"
        f.write_text("video-resolution = 720p\n")
        assert load_conf_file(str(f)) == {"resolution": "720p"}

    def test_video_resolution_underscore_alias(self, tmp_path):
        f = tmp_path / "coursera-dl.conf"
        f.write_text("video_resolution = 360p\n")
        assert load_conf_file(str(f)) == {"resolution": "360p"}

    def test_invalid_resolution_omitted(self, tmp_path):
        f = tmp_path / "coursera-dl.conf"
        f.write_text("video-resolution = 1080p\n")
        result = load_conf_file(str(f))
        assert "resolution" not in result

    def test_multiple_fields(self, tmp_path):
        f = tmp_path / "coursera-dl.conf"
        f.write_text("path = ./dl\nsubtitle-language = en\nvideo-resolution = 540p\n")
        assert load_conf_file(str(f)) == {
            "path": "./dl",
            "subtitle_language": "en",
            "resolution": "540p",
        }

    def test_last_alias_wins(self, tmp_path):
        # Both hyphen and underscore form present — last one should win
        f = tmp_path / "coursera-dl.conf"
        f.write_text("subtitle-language = en\nsubtitle_language = fr\n")
        result = load_conf_file(str(f))
        assert result["subtitle_language"] == "fr"

    def test_unknown_keys_ignored(self, tmp_path):
        f = tmp_path / "coursera-dl.conf"
        f.write_text("unknown-option = foo\npath = ./x\n")
        assert load_conf_file(str(f)) == {"path": "./x"}


# ---------------------------------------------------------------------------
# make_args
# ---------------------------------------------------------------------------

class TestMakeArgs:

    def test_file_formats_none_defaults_to_all(self):
        args = make_args(path=".", cookies_file=None, file_formats=None,
                         class_names=["ml"])
        assert args.file_formats == ["all"]

    def test_class_names_none_defaults_to_empty(self):
        args = make_args(path=".", cookies_file=None, class_names=None)
        assert args.class_names == []

    def test_specialization_always_false(self):
        args = make_args(path=".", cookies_file=None)
        assert args.specialization is False

    def test_assume_yes_always_true(self):
        args = make_args(path=".", cookies_file=None)
        assert args.assume_yes is True

    def test_explicit_values_round_trip(self):
        args = make_args(
            path="/tmp/courses",
            cookies_file="/tmp/cookies.txt",
            resolution="720p",
            subtitle_language="zh-CN",
            file_formats=["mp4", "pdf"],
            jobs=4,
            class_names=["ml", "nlp"],
            overwrite=True,
            download_quizzes=True,
            download_notebooks=True,
        )
        assert args.path == "/tmp/courses"
        assert args.cookies_file == "/tmp/cookies.txt"
        assert args.video_resolution == "720p"
        assert args.subtitle_language == "zh-CN"
        assert args.file_formats == ["mp4", "pdf"]
        assert args.jobs == 4
        assert args.class_names == ["ml", "nlp"]
        assert args.overwrite is True
        assert args.download_quizzes is True
        assert args.download_notebooks is True

    def test_returns_simple_namespace(self):
        args = make_args(path=".", cookies_file=None)
        assert isinstance(args, SimpleNamespace)

    def test_has_all_required_attributes(self):
        args = make_args(path=".", cookies_file=None)
        required = [
            "path", "cookies_file", "video_resolution", "subtitle_language",
            "file_formats", "jobs", "overwrite", "resume", "specialization",
            "assume_yes", "download_quizzes", "download_notebooks",
            "hooks", "mathjax_cdn_url",
        ]
        for attr in required:
            assert hasattr(args, attr), f"make_args() missing attribute: {attr}"
