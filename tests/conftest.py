"""Every test runs against a temporary root: no test may touch the real queue, data or YouTube."""
import pytest

from app.config import Settings

# Local .env choices (public uploads, LAN host, pinned channel) must not change test behavior;
# environment variables take precedence over the .env file.
LOCAL_DEFAULTS = {"HEADLINE_YOUTUBE_PRIVACY": "private", "HEADLINE_YOUTUBE_CHANNEL_ID": "",
                  "HEADLINE_HOST": "127.0.0.1", "HEADLINE_ALLOWED_HOSTS": "[]"}


@pytest.fixture(autouse=True)
def isolated_app_services(tmp_path, monkeypatch):
    for key, value in LOCAL_DEFAULTS.items():
        monkeypatch.setenv(key, value)
    # app.main builds its services from the real project root at import time. A test that runs
    # the app lifespan would otherwise recover and drain the live URL queue (and could upload).
    from app import main
    from app.integrations.youtube import YoutubeService
    from app.jobs.manager import Manager
    from app.jobs.submissions import SubmissionQueue

    config = Settings(root=tmp_path / "isolated-app", youtube_channel_id="")
    manager = Manager(config)
    youtube = YoutubeService(config)
    monkeypatch.setattr(main, "settings", config)
    monkeypatch.setattr(main, "manager", manager)
    monkeypatch.setattr(main, "youtube", youtube)
    monkeypatch.setattr(main, "submissions", SubmissionQueue(config, manager, youtube))
    yield
    manager.pool.shutdown(wait=False, cancel_futures=True)
