"""VideoGenService is a sealed stub in this build (no AI video provider)."""


def test_video_gen_is_never_available(monkeypatch):
    from backend.services import video_gen_service
    monkeypatch.setenv("FAL_KEY", "NOT-A-REAL-KEY")
    svc = video_gen_service.VideoGenService()
    assert svc.is_available() is False
    assert svc.generate_clip("a fox in the snow", "/tmp/clip.mp4") is None
    assert svc.generate_clip_parallel("a fox in the snow", "/tmp") == []
