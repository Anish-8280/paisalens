"""Private uploads on a public site: one visitor must never reach another's data."""
import os

import pytest
from fastapi.testclient import TestClient

from app.agent.loop import Settings
from app.limits import UsageLimiter
from app.main import create_app
from app.sessions import COOKIE_NAME, SessionManager, SessionsFull
from tests.test_agent import FakeClient, text
from tests.test_uploads import WRITE, as_csv, ruled_pdf


@pytest.fixture
def make_app(db_path, tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVATE_UPLOADS", "true")
    monkeypatch.delenv("ENABLE_UPLOADS", raising=False)
    monkeypatch.setenv("SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("UPLOADS_PER_MINUTE", "100")

    def build(*responses):
        settings = Settings(database_path=str(db_path), as_of="2026-11-02")
        return create_app(settings, FakeClient(*responses), UsageLimiter(100, 100))

    return build


def visitor(app):
    # A separate client is a separate browser: each keeps its own cookies.
    return TestClient(app, base_url="https://paisalens.example", raise_server_exceptions=False)


def upload(client, data=None, name="s.csv"):
    return client.post("/analyses", files={"file": (name, data or as_csv(), "text/csv")}, data={"account": "HDFC"}, headers=WRITE)


def test_visitors_cannot_reach_each_others_analyses_in_any_way(make_app):
    app = make_app(text("should never run"))
    alice, mallory = visitor(app), visitor(app)
    identifier = upload(alice).json()["analysis"]["id"]
    upload(mallory)

    assert identifier not in [a["id"] for a in mallory.get("/analyses").json()["analyses"]]
    attempts = [
        mallory.get("/stats", params={"analysis": identifier}),
        mallory.get("/transactions", params={"analysis": identifier}),
        mallory.post("/ask", json={"question": "x", "analysis": identifier}),
        mallory.patch(f"/analyses/{identifier}", json={"ai_allowed": True}, headers=WRITE),
        mallory.patch("/categories", json={"analysis": identifier, "merchant": "Swiggy", "category": "Other"}, headers=WRITE),
        mallory.delete(f"/analyses/{identifier}", headers=WRITE),
        mallory.post(f"/analyses/{identifier}/statements", files={"file": ("s.csv", as_csv(), "text/csv")}, headers=WRITE),
    ]
    assert [response.status_code for response in attempts] == [404] * len(attempts)
    # Alice's analysis came through all of that unchanged.
    analysis = alice.get("/analyses").json()["analyses"][0]
    assert analysis["id"] == identifier and analysis["transactions"] == 5 and analysis["ai_allowed"] is False


def test_a_visitor_without_a_cookie_sees_nothing_and_gets_no_session(make_app):
    app = make_app()
    alice, stranger = visitor(app), visitor(app)
    identifier = upload(alice).json()["analysis"]["id"]
    assert stranger.get("/analyses").json()["analyses"] == []
    assert stranger.get("/stats", params={"analysis": identifier}).status_code == 404
    assert "set-cookie" not in stranger.get("/analyses").headers
    assert stranger.get("/stats").status_code == 200  # the example is open to everyone


@pytest.mark.parametrize("forged", ["guess", "a" * 64, "x" * 5000, "../../etc/passwd"])
def test_made_up_cookies_lead_nowhere_and_are_not_adopted(make_app, forged):
    app = make_app()
    alice, attacker = visitor(app), visitor(app)
    identifier = upload(alice).json()["analysis"]["id"]
    attacker.cookies.set(COOKIE_NAME, forged, domain="paisalens.example")
    assert attacker.get("/analyses").json()["analyses"] == []
    assert attacker.get("/stats", params={"analysis": identifier}).status_code == 404
    # Uploading with a forged cookie must issue a fresh token rather than accept the attacker's choice.
    response = upload(attacker)
    assert response.status_code == 200
    assert forged not in response.headers["set-cookie"]


def test_the_folder_name_cannot_be_used_as_a_cookie(make_app, tmp_path):
    app = make_app()
    alice, attacker = visitor(app), visitor(app)
    upload(alice)
    folder = next((tmp_path / "sessions").iterdir()).name
    attacker.cookies.set(COOKIE_NAME, folder, domain="paisalens.example")
    assert attacker.get("/analyses").json()["analyses"] == []


def test_the_cookie_is_locked_down(make_app):
    header = upload(visitor(make_app())).headers["set-cookie"].lower()
    assert "httponly" in header and "secure" in header and "samesite=strict" in header
    assert "max-age" not in header and "expires" not in header  # gone when the browser session ends


def test_returning_visitors_keep_their_analyses_and_cookie(make_app):
    client = visitor(make_app())
    first = upload(client)
    second = upload(client)
    assert "set-cookie" in first.headers and "set-cookie" not in second.headers
    assert len(client.get("/analyses").json()["analyses"]) == 2


def test_a_bad_file_creates_no_session(make_app, tmp_path):
    client = visitor(make_app())
    response = upload(client, b"nonsense")
    assert response.status_code == 422 and "set-cookie" not in response.headers
    assert list((tmp_path / "sessions").iterdir()) == []


def test_writes_still_need_the_custom_header(make_app):
    client = visitor(make_app())
    response = client.post("/analyses", files={"file": ("s.csv", as_csv(), "text/csv")})
    assert response.status_code == 403 and "set-cookie" not in response.headers


def test_uploaded_files_are_not_kept(make_app, tmp_path):
    upload(visitor(make_app()), ruled_pdf(), "statement.pdf")
    files = [path for path in (tmp_path / "sessions").rglob("*") if path.is_file()]
    assert len(files) == 1 and files[0].suffix == ".db"


def test_visitor_limits(make_app, monkeypatch):
    client = visitor(make_app())
    assert [upload(client).status_code for _ in range(4)] == [200, 200, 200, 409]
    assert upload(client, b"x" * (5 * 1024 * 1024 + 1)).status_code == 409  # the analysis limit comes first
    other = visitor(make_app())
    assert upload(other, b"x" * (5 * 1024 * 1024 + 1)).status_code == 413


def test_upload_rate_limit(make_app, monkeypatch):
    monkeypatch.setenv("UPLOADS_PER_MINUTE", "1")
    client = visitor(make_app())
    assert upload(client).status_code == 200
    response = upload(client)
    assert response.status_code == 429 and response.headers["retry-after"] == "60"


def test_data_responses_are_never_cached_but_the_page_can_revalidate(make_app):
    client = visitor(make_app())
    upload(client)
    assert client.get("/analyses").headers["cache-control"] == "no-store"
    assert client.get("/stats").headers["cache-control"] == "no-store"
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert "max-age" in client.get("/").headers["strict-transport-security"]


def test_config_tells_the_page_what_kind_of_storage_this_is(make_app):
    config = visitor(make_app()).get("/config").json()
    assert config["uploads_enabled"] is True and config["storage"] == "temporary"
    assert config["max_upload_mb"] == 5 and config["retention_minutes"] == 120


def test_public_hosts_are_allowed_in_private_mode(make_app):
    assert visitor(make_app()).get("/config").status_code == 200


def test_both_upload_modes_together_are_refused(make_app, monkeypatch):
    monkeypatch.setenv("ENABLE_UPLOADS", "true")
    with pytest.raises(ValueError, match="not both"):
        make_app()


def test_uploads_stay_off_unless_asked_for(db_path, monkeypatch):
    monkeypatch.delenv("PRIVATE_UPLOADS", raising=False)
    monkeypatch.delenv("ENABLE_UPLOADS", raising=False)
    client = visitor(create_app(Settings(database_path=str(db_path)), FakeClient(), UsageLimiter(1, 1)))
    assert client.get("/config").json()["uploads_enabled"] is False
    assert upload(client).status_code == 404


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def test_inactive_sessions_are_deleted_and_activity_keeps_them(tmp_path):
    clock = Clock()
    manager = SessionManager(tmp_path, ttl_seconds=100, clock=clock)
    token, store = manager.create()
    store.create("x")[1].close()
    clock.now += 90
    assert manager.get(token) is not None  # this visit restarts the clock
    clock.now += 90
    manager.sweep()
    assert manager.get(token) is not None
    clock.now += 101
    manager.sweep()
    assert manager.get(token) is None
    assert list(tmp_path.iterdir()) == []


def test_an_expired_session_is_refused_even_before_a_sweep(tmp_path):
    clock = Clock()
    manager = SessionManager(tmp_path, ttl_seconds=100, clock=clock)
    token, _ = manager.create()
    clock.now += 101
    assert manager.get(token) is None and list(tmp_path.iterdir()) == []


def test_restarting_the_server_deletes_everything(tmp_path):
    token, store = SessionManager(tmp_path, ttl_seconds=100).create()
    store.create("x")[1].close()
    unrelated = tmp_path / "keep-me"
    unrelated.mkdir()
    restarted = SessionManager(tmp_path, ttl_seconds=100)
    assert restarted.get(token) is None
    assert [path.name for path in tmp_path.iterdir()] == ["keep-me"]  # only its own folders are removed


def test_visitor_cap_frees_up_when_sessions_expire(tmp_path):
    clock = Clock()
    manager = SessionManager(tmp_path, ttl_seconds=100, max_sessions=1, clock=clock)
    manager.create()
    with pytest.raises(SessionsFull):
        manager.create()
    clock.now += 101
    assert manager.create()[0]


def test_session_folders_are_private_to_the_server_user(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX permissions")
    token, _ = SessionManager(tmp_path, ttl_seconds=100).create()
    folder = next(tmp_path.iterdir())
    assert oct(folder.stat().st_mode)[-3:] == "700" and token not in folder.name


def test_full_server_is_explained(make_app, monkeypatch):
    monkeypatch.setenv("MAX_VISITORS", "1")
    app = make_app()
    assert upload(visitor(app)).status_code == 200
    assert upload(visitor(app)).status_code == 503
