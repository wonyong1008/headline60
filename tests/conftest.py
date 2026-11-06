"""Every test runs against a temporary root: no test may touch the real queue, data or YouTube."""
import pytest

from app.config import Settings


@pytest.fixture(autouse=True)
def isolated_app_services(tmp_path, monkeypatch):
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
