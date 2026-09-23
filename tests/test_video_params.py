"""纯函数与参数校验测试 — Agnes Video 2.5 payload 规则"""

import pytest

from agnes_ai_generation.models import TaskStatus
from agnes_ai_generation.service import (
    _is_video_flash,
    _normalize_text_model,
    _normalize_video_model,
    _parse_progress,
    _poll_path,
    build_video_payload,
    derive_mode,
    extract_video_urls,
    validate_ratio,
    validate_size,
    validate_video_args,
)


class TestNormalize:
    def test_dead_video_model_mapped_to_free(self):
        assert _normalize_video_model("agnes-video-v2.0") == "agnes-video-2.5-flash"
        assert _normalize_video_model("") == "agnes-video-2.5-flash"
        assert _normalize_video_model("agnes-video-2.5") == "agnes-video-2.5"

    def test_deprecated_text_model(self):
        assert _normalize_text_model("agnes-2.0-flash") == "agnes-2.5-flash"
        assert _normalize_text_model("agnes-3.0-flash") == "agnes-3.0-flash"

    def test_flash_detection(self):
        assert _is_video_flash("agnes-video-2.5-flash")
        assert not _is_video_flash("agnes-video-2.5")


class TestDeriveMode:
    def test_text(self):
        assert derive_mode() == "text"

    def test_keyframe(self):
        assert derive_mode(first_frame="a.png") == "keyframe"
        assert derive_mode(last_frame="b.png") == "keyframe"
        assert derive_mode(first_frame="a", images=["x"]) == "keyframe"  # frame 优先

    def test_reference(self):
        assert derive_mode(images=["a"]) == "reference"
        assert derive_mode(audios=["a"]) == "reference"
        assert derive_mode(videos=[{"url": "v"}]) == "reference"


class TestBuildPayload:
    def test_text_payload(self):
        p = build_video_payload("cat", "agnes-video-2.5-flash", "text", "5", "720P", "16:9")
        assert p == {
            "model": "agnes-video-2.5-flash", "prompt": "cat", "mode": "text",
            "seconds": "5", "size": "720P", "aspect_ratio": "16:9",
        }

    def test_keyframe_payload(self):
        p = build_video_payload("t", "m", "keyframe", "4", "720P", "1:1",
                                first_frame="a.png", last_frame="b.png", seed=42)
        assert p["first_frame"] == "a.png"
        assert p["last_frame"] == "b.png"
        assert p["seed"] == 42
        assert "images" not in p

    def test_reference_payload(self):
        p = build_video_payload("t", "m", "reference", "8", "1080P", "9:16",
                                images=["a"], audios=["b"], videos=[{"url": "c"}])
        assert p["images"] == ["a"]
        assert p["audios"] == ["b"]
        assert p["videos"] == [{"url": "c"}]
        assert "first_frame" not in p


class TestValidateVideoArgs:
    def test_ok(self):
        validate_video_args("text", "5", "720P", "agnes-video-2.5-flash")

    @pytest.mark.parametrize("ratio", ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"])
    def test_video_ratios(self, ratio):
        """合法比例集与 API 实测一致（2026-09-23 400 响应枚举）。"""
        validate_video_args("text", "5", "720P", "agnes-video-2.5", aspect_ratio=ratio)

    @pytest.mark.parametrize("ratio", ["3:2", "2:3", "5:4", ""])
    def test_bad_video_ratio(self, ratio):
        with pytest.raises(ValueError, match="aspect_ratio"):
            validate_video_args("text", "5", "720P", "agnes-video-2.5", aspect_ratio=ratio)

    def test_bad_mode(self):
        with pytest.raises(ValueError, match="mode"):
            validate_video_args("ti2vid", "5", "720P", "agnes-video-2.5-flash")

    @pytest.mark.parametrize("seconds", ["3", "13", "abc", "5.5"])
    def test_bad_seconds(self, seconds):
        with pytest.raises(ValueError, match="seconds"):
            validate_video_args("text", seconds, "720P", "agnes-video-2.5")

    def test_flash_only_720p(self):
        with pytest.raises(ValueError, match="720P"):
            validate_video_args("text", "5", "1080P", "agnes-video-2.5-flash")

    def test_flash_no_video_ref(self):
        with pytest.raises(ValueError, match="参考视频"):
            validate_video_args("reference", "5", "720P", "agnes-video-2.5-flash",
                                images=["a"], videos=[{"url": "v"}])

    def test_flash_max_5_images(self):
        with pytest.raises(ValueError, match="5 张"):
            validate_video_args("reference", "5", "720P", "agnes-video-2.5-flash",
                                images=list("abcdefgh"))

    def test_non_flash_allows_8_images(self):
        validate_video_args("reference", "5", "1080P", "agnes-video-2.5", images=list("abcdefgh"))

    def test_text_rejects_media(self):
        with pytest.raises(ValueError):
            validate_video_args("text", "5", "720P", "agnes-video-2.5", first_frame="a")

    def test_keyframe_requires_frame(self):
        with pytest.raises(ValueError, match="first_frame"):
            validate_video_args("keyframe", "5", "720P", "agnes-video-2.5")

    def test_keyframe_rejects_reference_media(self):
        with pytest.raises(ValueError, match="不允许"):
            validate_video_args("keyframe", "5", "720P", "agnes-video-2.5",
                                first_frame="a", images=["b"])

    def test_reference_requires_media(self):
        with pytest.raises(ValueError, match="至少"):
            validate_video_args("reference", "5", "720P", "agnes-video-2.5")


class TestValidateImageSize:
    def test_tiers(self):
        for s in ("1K", "2K", "3K", "4K", "1k"):
            validate_size(s)

    def test_legacy_exact(self):
        validate_size("1024x768")

    def test_bad(self):
        with pytest.raises(ValueError):
            validate_size("8k")

    def test_ratios(self):
        validate_ratio("16:9")
        validate_ratio(None)
        with pytest.raises(ValueError):
            validate_ratio("5:4")


class TestVideoUrlExtraction:
    def test_top_level_url_25(self):
        assert extract_video_urls({"status": "completed", "url": "https://x/v.mp4"}) == ["https://x/v.mp4"]

    def test_metadata_url(self):
        assert extract_video_urls({"metadata": {"url": "https://x/v.mp4"}}) == ["https://x/v.mp4"]

    def test_dedup_and_non_http(self):
        urls = extract_video_urls({"url": "https://a", "video_url": "https://a", "remixed_from_video_id": "vid123"})
        assert urls == ["https://a"]

    def test_data_array(self):
        assert extract_video_urls({"data": [{"url": "https://b"}]}) == ["https://b"]


class TestPollPath:
    def test_includes_model_name(self):
        path = _poll_path("vid_123", "agnes-video-2.5-flash")
        assert path == "/agnesapi?video_id=vid_123&model_name=agnes-video-2.5-flash"


class TestParseProgress:
    def test_fraction(self):
        assert _parse_progress(0.5) == 50

    def test_percent(self):
        assert _parse_progress(72) == 72

    def test_junk(self):
        assert _parse_progress(None) == 0
        assert _parse_progress("abc") == 0


class TestTaskStatusFromApi:
    def test_mapping(self):
        assert TaskStatus.from_api("queued") == TaskStatus.QUEUED
        assert TaskStatus.from_api("in_progress") == TaskStatus.PROCESSING
        assert TaskStatus.from_api("completed") == TaskStatus.COMPLETED
        assert TaskStatus.from_api("failed") == TaskStatus.FAILED
        assert TaskStatus.from_api("") == TaskStatus.PROCESSING
