"""Tests for resume sessions and launch new sessions features."""

import asyncio
import json
import os
import signal
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Patch lifespan before importing app so it doesn't scan ~/.claude
from contextlib import asynccontextmanager


@asynccontextmanager
async def _noop_lifespan(app):
    yield


# Patch before import
import server as server_module

server_module.lifespan = _noop_lifespan
server_module.app.router.lifespan_context = _noop_lifespan

from server import (
    DashboardState,
    ManagedSession,
    app,
    manager,
    state,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_state():
    """Reset global dashboard state between tests."""
    state.sessions.clear()
    state.machines.clear()
    state.file_offsets.clear()
    state.session_project_map.clear()
    state.managed_sessions.clear()
    manager.active_connections.clear()
    yield
    # Cleanup managed sessions
    for ms in list(state.managed_sessions.values()):
        if ms.process and ms.process.returncode is None:
            try:
                ms.process.kill()
            except Exception:
                pass
    state.managed_sessions.clear()


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# ManagedSession unit tests
# ---------------------------------------------------------------------------


class TestManagedSessionInit:
    def test_accepts_none_session_id(self):
        ms = ManagedSession(session_id=None, project_path="/tmp/test")
        assert ms.session_id is None
        assert ms.project_path == "/tmp/test"
        assert ms.ssh_host is None
        assert ms.status == "starting"

    def test_accepts_string_session_id(self):
        ms = ManagedSession(session_id="abc-123", project_path="/tmp/test")
        assert ms.session_id == "abc-123"

    def test_session_id_event_not_set_initially(self):
        ms = ManagedSession(session_id=None, project_path="/tmp/test")
        assert not ms._session_id_event.is_set()

    def test_session_id_event_set_for_existing(self):
        ms = ManagedSession(session_id="abc", project_path="/tmp/test")
        # For existing sessions, event is NOT pre-set (only set on discovery)
        assert not ms._session_id_event.is_set()


class TestExtractSessionId:
    def test_system_event(self):
        ms = ManagedSession(session_id=None, project_path="/tmp")
        sid = ms._extract_session_id({
            "type": "system",
            "session_id": "sess-abc-123",
        })
        assert sid == "sess-abc-123"

    def test_result_event(self):
        ms = ManagedSession(session_id=None, project_path="/tmp")
        sid = ms._extract_session_id({
            "type": "result",
            "session_id": "sess-result-456",
            "is_error": False,
        })
        assert sid == "sess-result-456"

    def test_top_level_session_id(self):
        ms = ManagedSession(session_id=None, project_path="/tmp")
        sid = ms._extract_session_id({
            "type": "assistant",
            "session_id": "sess-top-789",
        })
        assert sid == "sess-top-789"

    def test_no_session_id(self):
        ms = ManagedSession(session_id=None, project_path="/tmp")
        sid = ms._extract_session_id({
            "type": "assistant",
            "message": {"content": "hello"},
        })
        assert sid is None

    def test_empty_session_id(self):
        ms = ManagedSession(session_id=None, project_path="/tmp")
        sid = ms._extract_session_id({
            "type": "system",
            "session_id": "",
        })
        assert sid is None


class TestDetectWaiting:
    def test_result_sets_waiting(self):
        ms = ManagedSession(session_id="test", project_path="/tmp")
        ms.status = "running"
        ms._detect_waiting({"type": "result", "is_error": False})
        assert ms.status == "waiting"

    def test_result_error_sets_error(self):
        ms = ManagedSession(session_id="test", project_path="/tmp")
        ms.status = "running"
        ms._detect_waiting({"type": "result", "is_error": True})
        assert ms.status == "error"

    def test_assistant_sets_running(self):
        ms = ManagedSession(session_id="test", project_path="/tmp")
        ms.status = "waiting"
        ms._detect_waiting({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hi"}]},
        })
        assert ms.status == "running"

    def test_ask_user_question_sets_waiting(self):
        ms = ManagedSession(session_id="test", project_path="/tmp")
        ms.status = "running"
        ms._detect_waiting({
            "type": "assistant",
            "message": {
                "stop_reason": "tool_use",
                "content": [{"type": "tool_use", "name": "AskUserQuestion"}],
            },
        })
        assert ms.status == "waiting"


class TestManagedSessionStart:
    @pytest.mark.asyncio
    async def test_start_local_no_resume(self):
        ms = ManagedSession(session_id=None, project_path="/tmp")
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.pid = 12345
            mock_proc.stdout = AsyncMock()
            mock_proc.stderr = AsyncMock()
            mock_proc.stdin = AsyncMock()
            mock_proc.returncode = None
            # readline returns empty to end the loop
            mock_proc.stdout.readline = AsyncMock(return_value=b"")
            mock_proc.stderr.readline = AsyncMock(return_value=b"")
            mock_exec.return_value = mock_proc

            await ms.start(resume=False)

            assert ms.status == "running"
            assert ms.process is mock_proc
            # Verify --resume was NOT passed
            call_args = mock_exec.call_args
            cmd_list = call_args[0]
            assert "--resume" not in cmd_list

    @pytest.mark.asyncio
    async def test_start_local_with_resume(self):
        ms = ManagedSession(session_id="test-session", project_path="/tmp")
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.pid = 12345
            mock_proc.stdout = AsyncMock()
            mock_proc.stderr = AsyncMock()
            mock_proc.stdin = AsyncMock()
            mock_proc.returncode = None
            mock_proc.stdout.readline = AsyncMock(return_value=b"")
            mock_proc.stderr.readline = AsyncMock(return_value=b"")
            mock_exec.return_value = mock_proc

            await ms.start(resume=True)

            assert ms.status == "running"
            call_args = mock_exec.call_args
            cmd_list = call_args[0]
            assert "--resume" in cmd_list
            assert "test-session" in cmd_list

    @pytest.mark.asyncio
    async def test_start_no_resume_without_session_id(self):
        """When session_id is None and resume=True, --resume should NOT be passed."""
        ms = ManagedSession(session_id=None, project_path="/tmp")
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.pid = 12345
            mock_proc.stdout = AsyncMock()
            mock_proc.stderr = AsyncMock()
            mock_proc.stdin = AsyncMock()
            mock_proc.returncode = None
            mock_proc.stdout.readline = AsyncMock(return_value=b"")
            mock_proc.stderr.readline = AsyncMock(return_value=b"")
            mock_exec.return_value = mock_proc

            await ms.start(resume=True)

            call_args = mock_exec.call_args
            cmd_list = call_args[0]
            assert "--resume" not in cmd_list


class TestSessionIdDiscovery:
    @pytest.mark.asyncio
    async def test_discover_session_id_from_stream(self):
        """Simulate stream output delivering a session_id."""
        ms = ManagedSession(session_id=None, project_path="/tmp")
        events = [
            json.dumps({"type": "system", "session_id": "discovered-id-123"}) + "\n",
            "",  # empty line to end
        ]
        event_iter = iter(events)

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.pid = 99
            mock_proc.returncode = None

            async def fake_readline():
                try:
                    return next(event_iter).encode()
                except StopIteration:
                    return b""

            mock_proc.stdout = MagicMock()
            mock_proc.stdout.readline = fake_readline
            mock_proc.stderr = MagicMock()
            mock_proc.stderr.readline = AsyncMock(return_value=b"")
            mock_proc.stdin = AsyncMock()
            mock_exec.return_value = mock_proc

            await ms.start(resume=False)
            # Wait for the read loop to process
            await asyncio.sleep(0.1)

            assert ms.session_id == "discovered-id-123"
            assert ms._session_id_event.is_set()


class TestPendingMessages:
    def test_get_pending_messages_empty(self):
        ms = ManagedSession(session_id="test", project_path="/tmp")
        assert ms.get_pending_messages() == []

    def test_get_pending_messages_with_user(self):
        ms = ManagedSession(session_id="test", project_path="/tmp")
        ms._pending_user = {
            "role": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "content": [{"type": "text", "text": "hello"}],
        }
        msgs = ms.get_pending_messages()
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    def test_get_pending_messages_with_both(self):
        ms = ManagedSession(session_id="test", project_path="/tmp")
        ms._pending_user = {
            "role": "user",
            "timestamp": "2026-01-01T00:00:00Z",
            "content": [{"type": "text", "text": "hello"}],
        }
        ms._pending_assistant = {
            "role": "assistant",
            "timestamp": "2026-01-01T00:00:01Z",
            "content": [{"type": "text", "text": "hi"}],
        }
        msgs = ms.get_pending_messages()
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


class TestGetProjects:
    @pytest.mark.asyncio
    async def test_empty(self, client):
        resp = await client.get("/api/projects")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_returns_sorted_unique(self, client):
        state.sessions["s1"] = {
            "session_id": "s1",
            "project_path": "/home/user/b-project",
        }
        state.sessions["s2"] = {
            "session_id": "s2",
            "project_path": "/home/user/a-project",
        }
        state.sessions["s3"] = {
            "session_id": "s3",
            "project_path": "/home/user/b-project",  # duplicate
        }
        resp = await client.get("/api/projects")
        assert resp.status_code == 200
        data = resp.json()
        assert data == ["/home/user/a-project", "/home/user/b-project"]

    @pytest.mark.asyncio
    async def test_skips_empty_paths(self, client):
        state.sessions["s1"] = {"session_id": "s1", "project_path": ""}
        state.sessions["s2"] = {
            "session_id": "s2",
            "project_path": "/good/path",
        }
        resp = await client.get("/api/projects")
        assert resp.json() == ["/good/path"]


class TestTakeOverInactive:
    @pytest.mark.asyncio
    async def test_takeover_inactive_session(self, client):
        """Take-over on an inactive session should work (kill is no-op)."""
        state.sessions["inactive-sess"] = {
            "session_id": "inactive-sess",
            "project_path": "/tmp/test",
            "machine": "local",
            "is_active": False,
        }

        with patch("server.get_running_session_map", return_value={}), \
             patch.object(ManagedSession, "start", new_callable=AsyncMock) as mock_start:
            mock_start.side_effect = lambda resume: setattr(
                state.managed_sessions["inactive-sess"], "status", "running"
            )

            resp = await client.post("/api/sessions/inactive-sess/take-over")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        # Managed session was created
        assert "inactive-sess" in state.managed_sessions

    @pytest.mark.asyncio
    async def test_takeover_active_session_kills_pid(self, client):
        """Take-over on active session should attempt to kill the PID."""
        state.sessions["active-sess"] = {
            "session_id": "active-sess",
            "project_path": "/tmp/test",
            "machine": "local",
            "is_active": True,
        }

        with patch("server.get_running_session_map", return_value={"active-sess": "999"}), \
             patch("os.kill") as mock_kill, \
             patch.object(ManagedSession, "start", new_callable=AsyncMock):

            resp = await client.post("/api/sessions/active-sess/take-over")

        assert resp.status_code == 200
        mock_kill.assert_called_once_with(999, signal.SIGINT)

    @pytest.mark.asyncio
    async def test_takeover_already_managed(self, client):
        """Take-over on already managed session should return already_managed."""
        state.sessions["managed-sess"] = {
            "session_id": "managed-sess",
            "project_path": "/tmp/test",
            "machine": "local",
        }
        ms = ManagedSession("managed-sess", "/tmp/test")
        ms.status = "running"
        state.managed_sessions["managed-sess"] = ms

        resp = await client.post("/api/sessions/managed-sess/take-over")
        assert resp.status_code == 200
        assert resp.json()["status"] == "already_managed"

    @pytest.mark.asyncio
    async def test_takeover_nonexistent_session(self, client):
        resp = await client.post("/api/sessions/nonexistent/take-over")
        assert resp.status_code == 404


class TestNewSession:
    @pytest.mark.asyncio
    async def test_missing_project_path(self, client):
        resp = await client.post(
            "/api/sessions/new",
            json={"project_path": "", "prompt": "hello"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_success(self, client):
        """New session should create managed session and return session_id."""
        # Use autospec=True so side_effect receives `self` (the ManagedSession instance)
        with patch.object(ManagedSession, "start", autospec=True) as mock_start, \
             patch.object(ManagedSession, "send_message", autospec=True) as mock_send:

            async def patched_start(self, resume=False):
                self.status = "running"
                self.session_id = "new-session-id-abc"
                self._session_id_event.set()

            async def patched_send(self, text):
                self._pending_user = {
                    "role": "user",
                    "content": [{"type": "text", "text": text}],
                }

            mock_start.side_effect = patched_start
            mock_send.side_effect = patched_send

            resp = await client.post(
                "/api/sessions/new",
                json={"project_path": "/tmp/myproject", "prompt": "build a thing"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["session_id"] == "new-session-id-abc"
        assert "new-session-id-abc" in state.managed_sessions
        assert "new-session-id-abc" in state.sessions
        session_data = state.sessions["new-session-id-abc"]
        assert session_data["project_path"] == "/tmp/myproject"
        assert session_data["project_name"] == "myproject"
        assert session_data["is_active"] is True

    @pytest.mark.asyncio
    async def test_timeout(self, client):
        """If session_id is never discovered, should return 500."""
        with patch.object(ManagedSession, "start", autospec=True) as mock_start, \
             patch.object(ManagedSession, "send_message", autospec=True), \
             patch.object(ManagedSession, "stop", autospec=True), \
             patch("server.asyncio.wait_for", side_effect=asyncio.TimeoutError):

            async def patched_start(self, resume=False):
                self.status = "running"

            mock_start.side_effect = patched_start

            resp = await client.post(
                "/api/sessions/new",
                json={"project_path": "/tmp/proj", "prompt": "test"},
            )

        assert resp.status_code == 500
        assert "Timed out" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_local_machine_default(self, client):
        """Machine should default to 'local' when not specified."""
        with patch.object(ManagedSession, "start", autospec=True) as mock_start, \
             patch.object(ManagedSession, "send_message", autospec=True):

            async def patched_start(self, resume=False):
                self.status = "running"
                self.session_id = "local-session"
                self._session_id_event.set()

            mock_start.side_effect = patched_start

            resp = await client.post(
                "/api/sessions/new",
                json={"project_path": "/tmp/proj", "prompt": "test"},
            )

        data = resp.json()
        assert data["status"] == "ok"
        session_data = state.sessions["local-session"]
        assert session_data["machine"] == "local"


class TestMessagesForManagedSession:
    @pytest.mark.asyncio
    async def test_messages_returns_empty_for_new_managed(self, client):
        """GET messages for a managed session whose JSONL doesn't exist yet."""
        state.sessions["new-managed"] = {
            "session_id": "new-managed",
            "machine": "local",
            "project_path": "/tmp/test",
        }
        ms = ManagedSession("new-managed", "/tmp/test")
        ms.status = "running"
        state.managed_sessions["new-managed"] = ms

        with patch("server.find_session_file", return_value=None):
            resp = await client.get("/api/sessions/new-managed/messages")

        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "new-managed"
        assert data["messages"] == []

    @pytest.mark.asyncio
    async def test_messages_404_for_unmanaged_missing_file(self, client):
        """GET messages for unmanaged session with no file should 404."""
        state.sessions["orphan"] = {
            "session_id": "orphan",
            "machine": "local",
            "project_path": "/tmp/test",
        }

        with patch("server.find_session_file", return_value=None):
            resp = await client.get("/api/sessions/orphan/messages")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_messages_includes_pending(self, client):
        """Messages API should include pending messages from managed session."""
        # Create a temp JSONL file with no messages
        with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as f:
            f.write("")
            tmp_path = f.name

        try:
            state.sessions["pending-test"] = {
                "session_id": "pending-test",
                "machine": "local",
                "project_path": "/tmp/test",
            }
            ms = ManagedSession("pending-test", "/tmp/test")
            ms.status = "running"
            ms._pending_user = {
                "role": "user",
                "timestamp": "2026-01-01T00:00:00Z",
                "content": [{"type": "text", "text": "my pending message"}],
            }
            ms._pending_assistant = {
                "role": "assistant",
                "timestamp": "2026-01-01T00:00:01Z",
                "content": [{"type": "text", "text": "assistant response"}],
                "model": "test-model",
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
            state.managed_sessions["pending-test"] = ms

            with patch("server.find_session_file", return_value=tmp_path):
                resp = await client.get("/api/sessions/pending-test/messages")

            assert resp.status_code == 200
            data = resp.json()
            msgs = data["messages"]
            assert len(msgs) == 2
            assert msgs[0]["role"] == "user"
            assert msgs[0]["content"][0]["text"] == "my pending message"
            assert msgs[1]["role"] == "assistant"
        finally:
            os.unlink(tmp_path)


class TestNewSessionRemote:
    @pytest.mark.asyncio
    async def test_remote_machine(self, client):
        """New session with machine != 'local' should set ssh_host."""
        with patch.object(ManagedSession, "start", autospec=True) as mock_start, \
             patch.object(ManagedSession, "send_message", autospec=True):

            captured_ms = []

            async def patched_start(self, resume=False):
                captured_ms.append(self)
                self.status = "running"
                self.session_id = "remote-session-abc"
                self._session_id_event.set()

            mock_start.side_effect = patched_start

            resp = await client.post(
                "/api/sessions/new",
                json={
                    "project_path": "/home/user/project",
                    "prompt": "do stuff",
                    "machine": "gpu-box",
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "remote-session-abc"
        assert len(captured_ms) == 1
        assert captured_ms[0].ssh_host == "gpu-box"
        assert state.sessions["remote-session-abc"]["machine"] == "gpu-box"

    @pytest.mark.asyncio
    async def test_local_machine_explicit(self, client):
        """Passing machine='local' should set ssh_host=None."""
        with patch.object(ManagedSession, "start", autospec=True) as mock_start, \
             patch.object(ManagedSession, "send_message", autospec=True):

            captured_ms = []

            async def patched_start(self, resume=False):
                captured_ms.append(self)
                self.status = "running"
                self.session_id = "explicit-local"
                self._session_id_event.set()

            mock_start.side_effect = patched_start

            resp = await client.post(
                "/api/sessions/new",
                json={
                    "project_path": "/tmp/proj",
                    "prompt": "test",
                    "machine": "local",
                },
            )

        assert resp.status_code == 200
        assert len(captured_ms) == 1
        assert captured_ms[0].ssh_host is None


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_send_no_process(self):
        """send_message with no process should be a no-op."""
        ms = ManagedSession(session_id="test", project_path="/tmp")
        ms.process = None
        await ms.send_message("hello")
        assert ms._pending_user is None

    @pytest.mark.asyncio
    async def test_send_exited_process(self):
        """send_message on exited process should be a no-op."""
        ms = ManagedSession(session_id="test", project_path="/tmp")
        ms.process = MagicMock()
        ms.process.stdin = MagicMock()
        ms.process.returncode = 1  # exited
        await ms.send_message("hello")
        assert ms._pending_user is None

    @pytest.mark.asyncio
    async def test_send_success(self):
        """send_message should write to stdin and set pending user."""
        ms = ManagedSession(session_id="test", project_path="/tmp")
        ms.process = MagicMock()
        ms.process.stdin = MagicMock()
        ms.process.stdin.write = MagicMock()
        ms.process.stdin.drain = AsyncMock()
        ms.process.returncode = None

        await ms.send_message("hello world")

        assert ms._pending_user is not None
        assert ms._pending_user["content"][0]["text"] == "hello world"
        assert ms.status == "running"
        # Verify the JSON written to stdin
        written = ms.process.stdin.write.call_args[0][0]
        parsed = json.loads(written.decode().strip())
        assert parsed["type"] == "user"
        assert parsed["message"]["content"] == "hello world"


class TestStopManagedSession:
    @pytest.mark.asyncio
    async def test_stop_no_process(self):
        """Stopping a session with no process should just set status."""
        ms = ManagedSession(session_id="test", project_path="/tmp")
        ms.process = None
        await ms.stop()
        assert ms.status == "stopped"

    @pytest.mark.asyncio
    async def test_stop_with_process(self):
        """Stopping should send SIGINT then wait."""
        ms = ManagedSession(session_id="test", project_path="/tmp")
        ms.process = MagicMock()
        ms.process.send_signal = MagicMock()
        ms.process.wait = AsyncMock()
        ms.process.kill = MagicMock()
        ms._read_task = None
        ms._stderr_task = None

        await ms.stop()

        ms.process.send_signal.assert_called_once_with(signal.SIGINT)
        assert ms.status == "stopped"


class TestNewSessionProjectName:
    @pytest.mark.asyncio
    async def test_project_name_extracted(self, client):
        """project_name should be basename of project_path."""
        with patch.object(ManagedSession, "start", autospec=True) as mock_start, \
             patch.object(ManagedSession, "send_message", autospec=True):

            async def patched_start(self, resume=False):
                self.status = "running"
                self.session_id = "proj-name-test"
                self._session_id_event.set()

            mock_start.side_effect = patched_start

            resp = await client.post(
                "/api/sessions/new",
                json={
                    "project_path": "/Users/dev/my-awesome-project",
                    "prompt": "test",
                },
            )

        data = resp.json()
        assert data["status"] == "ok"
        assert state.sessions["proj-name-test"]["project_name"] == "my-awesome-project"


class TestExtractPendingAssistant:
    def test_extracts_text_blocks(self):
        ms = ManagedSession(session_id="test", project_path="/tmp")
        ms._extract_pending_assistant({
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Hello there!"}],
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        })
        assert ms._pending_assistant is not None
        assert ms._pending_assistant["role"] == "assistant"
        assert ms._pending_assistant["model"] == "claude-sonnet-4-6"
        assert ms._pending_assistant["content"][0]["text"] == "Hello there!"

    def test_extracts_tool_use(self):
        ms = ManagedSession(session_id="test", project_path="/tmp")
        ms._extract_pending_assistant({
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
                ],
                "model": "claude-sonnet-4-6",
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })
        assert ms._pending_assistant is not None
        blocks = ms._pending_assistant["content"]
        assert blocks[0]["type"] == "tool_use"
        assert blocks[0]["name"] == "Bash"
        assert blocks[0]["input_preview"] == "ls -la"

    def test_ignores_empty_content(self):
        ms = ManagedSession(session_id="test", project_path="/tmp")
        ms._extract_pending_assistant({
            "type": "assistant",
            "message": {
                "content": [],
                "model": "test",
                "usage": {},
            },
        })
        assert ms._pending_assistant is None


class TestInactiveSessions:
    """Tests for inactive session visibility and resume flow."""

    @pytest.mark.asyncio
    async def test_get_sessions_returns_inactive(self, client):
        """GET /api/sessions should return both active and inactive sessions."""
        state.sessions["active-1"] = {
            "session_id": "active-1",
            "project_path": "/tmp/proj",
            "machine": "local",
            "is_active": True,
            "message_count": 5,
        }
        state.sessions["inactive-1"] = {
            "session_id": "inactive-1",
            "project_path": "/tmp/proj2",
            "machine": "local",
            "is_active": False,
            "message_count": 10,
        }
        resp = await client.get("/api/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        sids = {s["session_id"] for s in data}
        assert "active-1" in sids
        assert "inactive-1" in sids

    @pytest.mark.asyncio
    async def test_get_session_detail_inactive(self, client):
        """GET /api/sessions/{id} works for inactive sessions."""
        state.sessions["inactive-detail"] = {
            "session_id": "inactive-detail",
            "project_path": "/tmp/proj",
            "machine": "local",
            "is_active": False,
            "message_count": 3,
        }
        resp = await client.get("/api/sessions/inactive-detail")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "inactive-detail"
        assert data["is_active"] is False

    @pytest.mark.asyncio
    async def test_resume_inactive_session(self, client):
        """POST take-over on inactive session should create managed session (resume flow)."""
        state.sessions["resume-test"] = {
            "session_id": "resume-test",
            "project_path": "/tmp/test",
            "machine": "local",
            "is_active": False,
            "message_count": 10,
        }

        with patch("server.get_running_session_map", return_value={}), \
             patch.object(ManagedSession, "start", new_callable=AsyncMock) as mock_start:
            mock_start.side_effect = lambda resume: setattr(
                state.managed_sessions["resume-test"], "status", "running"
            )

            resp = await client.post("/api/sessions/resume-test/take-over")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "resume-test" in state.managed_sessions

    @pytest.mark.asyncio
    async def test_messages_endpoint_works_for_inactive(self, client):
        """GET messages should work for inactive sessions with existing JSONL files."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".jsonl", mode="w", delete=False) as f:
            f.write(json.dumps({
                "type": "user",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": "hello from inactive"},
            }) + "\n")
            tmp_path = f.name

        try:
            state.sessions["inactive-msgs"] = {
                "session_id": "inactive-msgs",
                "project_path": "/tmp/test",
                "machine": "local",
                "is_active": False,
            }

            with patch("server.find_session_file", return_value=tmp_path):
                resp = await client.get("/api/sessions/inactive-msgs/messages")

            assert resp.status_code == 200
            data = resp.json()
            assert data["session_id"] == "inactive-msgs"
            assert len(data["messages"]) > 0
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_stats_include_inactive(self, client):
        """Stats should count both active and inactive sessions."""
        state.sessions["stat-active"] = {
            "session_id": "stat-active",
            "is_active": True,
            "message_count": 5,
            "estimated_cost_usd": 1.0,
            "total_input_tokens": 100,
            "total_output_tokens": 50,
        }
        state.sessions["stat-inactive"] = {
            "session_id": "stat-inactive",
            "is_active": False,
            "message_count": 10,
            "estimated_cost_usd": 2.0,
            "total_input_tokens": 200,
            "total_output_tokens": 100,
        }

        resp = await client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_sessions"] == 2
        assert data["active_sessions"] == 1
        assert data["total_messages"] == 15


class TestRemoteScan:
    """Tests for remote inactive session discovery."""

    @pytest.mark.asyncio
    async def test_scan_remote_machine_returns_sessions(self):
        """scan_remote_machine should return parsed sessions tagged with hostname."""
        from remote_monitor import scan_remote_machine

        fake_sessions = [
            {"session_id": "remote-1", "message_count": 5, "is_active": False},
            {"session_id": "remote-2", "message_count": 3, "is_active": False},
        ]

        with patch("remote_monitor.asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(
                return_value=(json.dumps(fake_sessions).encode(), b"")
            )
            mock_exec.return_value = mock_proc

            result = await scan_remote_machine("gpu-box", timeout=10)

        assert len(result) == 2
        assert result[0]["session_id"] == "remote-1"
        assert result[0]["machine"] == "gpu-box"
        assert result[1]["machine"] == "gpu-box"

    @pytest.mark.asyncio
    async def test_scan_remote_machine_timeout(self):
        """scan_remote_machine should return empty list on timeout."""
        from remote_monitor import scan_remote_machine

        with patch("remote_monitor.asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
            mock_exec.return_value = mock_proc

            result = await scan_remote_machine("slow-box", timeout=1)

        assert result == []

    @pytest.mark.asyncio
    async def test_scan_remote_machine_ssh_failure(self):
        """scan_remote_machine should return empty list on SSH failure."""
        from remote_monitor import scan_remote_machine

        with patch("remote_monitor.asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.returncode = 255
            mock_proc.communicate = AsyncMock(return_value=(b"", b"Connection refused"))
            mock_exec.return_value = mock_proc

            result = await scan_remote_machine("down-box")

        assert result == []

    @pytest.mark.asyncio
    async def test_periodic_scan_does_not_overwrite_active(self):
        """periodic_remote_scan should not overwrite sessions marked active by poll."""
        from server import periodic_remote_scan

        # Pre-populate an active session
        state.sessions["active-remote"] = {
            "session_id": "active-remote",
            "machine": "gpu-box",
            "is_active": True,
            "message_count": 5,
            "project_path": "/tmp/proj",
        }

        scan_result = [
            {"session_id": "active-remote", "message_count": 5, "is_active": False},
            {"session_id": "new-inactive", "message_count": 3, "is_active": False},
        ]

        with patch("server.load_config", return_value={
            "remote_machines": ["gpu-box"],
            "scan_interval_seconds": 999,
        }), patch("server.scan_remote_machine", new_callable=AsyncMock, return_value=scan_result):
            # Run one iteration of the scan loop by patching asyncio.sleep to raise
            # after the first iteration
            call_count = 0
            original_sleep = asyncio.sleep

            async def fake_sleep(seconds):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    # Initial 10s delay — skip
                    return
                # After first scan iteration, break out
                raise asyncio.CancelledError()

            with patch("server.asyncio.sleep", side_effect=fake_sleep):
                try:
                    await periodic_remote_scan()
                except asyncio.CancelledError:
                    pass

        # Active session should NOT have been overwritten
        assert state.sessions["active-remote"]["is_active"] is True

        # New inactive session should have been added
        assert "new-inactive" in state.sessions
        assert state.sessions["new-inactive"]["is_active"] is False
        assert state.sessions["new-inactive"]["machine"] == "gpu-box"


class TestExistingEndpointsStillWork:
    @pytest.mark.asyncio
    async def test_get_sessions_empty(self, client):
        resp = await client.get("/api/sessions")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_get_sessions_with_data(self, client):
        state.sessions["s1"] = {"session_id": "s1", "is_active": True}
        resp = await client.get("/api/sessions")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    @pytest.mark.asyncio
    async def test_get_session_not_found(self, client):
        resp = await client.get("/api/sessions/nope")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_stats(self, client):
        resp = await client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_sessions" in data
        assert "active_sessions" in data

    @pytest.mark.asyncio
    async def test_get_machines(self, client):
        resp = await client.get("/api/machines")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.asyncio
    async def test_send_to_unmanaged_session(self, client):
        resp = await client.post(
            "/api/sessions/nonexistent/send",
            json={"message": "hello"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_stop_nonexistent_session(self, client):
        resp = await client.post("/api/sessions/nonexistent/stop")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_interrupt_nonexistent_session(self, client):
        resp = await client.post("/api/sessions/nonexistent/interrupt")
        assert resp.status_code == 404
