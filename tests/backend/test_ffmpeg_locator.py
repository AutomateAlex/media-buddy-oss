from backend.lib import ffmpeg_locator


def test_video_encoder_args_ignores_disabled_libx264(monkeypatch):
    monkeypatch.setattr(
        ffmpeg_locator,
        "_encoders_output",
        lambda _ffmpeg: "\n".join(
            [
                "configuration: --disable-libx264 --enable-libopenh264",
                " V....D h264_mf              H264 via MediaFoundation (codec h264)",
                " V.S..D mpeg4                MPEG-4 part 2",
            ]
        ),
    )

    args = ffmpeg_locator.video_encoder_args("ffmpeg")

    assert args[:2] != ["-c:v", "libx264"]
