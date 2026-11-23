import io
import stat
import sys
from types import SimpleNamespace

import httpx
import pytest

from services import codebase_sources as sources


@pytest.fixture
def kudu(monkeypatch):
    monkeypatch.setattr(sources.settings, "AZURE_APP_SERVICE_NAME", "sample-app")
    monkeypatch.setattr(sources.settings, "KUDU_USERNAME", "synthetic-user")
    monkeypatch.setattr(sources.settings, "KUDU_PASSWORD", "synthetic-password")
    monkeypatch.setattr(sources.settings, "AZURE_CODEBASE_PATH", "site/wwwroot")
    requests = []
    options = {}
    responses = {}
    client = httpx.Client

    def handler(request):
        requests.append(request)
        return responses.get(request.url.path, httpx.Response(404))

    def factory(**kwargs):
        options.update(kwargs)
        return client(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(sources.httpx, "Client", factory)
    return requests, options, responses


@pytest.mark.parametrize("path", [
    "site/wwwroot", r"C:\home\site\wwwroot", "/home/site/wwwroot",
])
def test_kudu_normalization_and_get_only(kudu, monkeypatch, path):
    requests, options, responses = kudu
    monkeypatch.setattr(sources.settings, "AZURE_CODEBASE_PATH", path)
    responses["/api/vfs/site/wwwroot/"] = httpx.Response(200, json=[{
        "name": "worker.py", "mime": "text/plain", "size": 8,
        "href": "https://untrusted.example/worker.py",
    }])
    responses["/api/vfs/site/wwwroot/worker.py"] = httpx.Response(200, content=b"print(1)")
    with sources.open_source("azure") as source:
        assert source.list_directory("")[0].name == "worker.py"
        assert source.read_file("worker.py") == b"print(1)"
    assert all(request.method == "GET" for request in requests)
    assert all(request.url.host == "sample-app.scm.azurewebsites.net" for request in requests)
    assert options["follow_redirects"] is False
    assert options["trust_env"] is False


@pytest.mark.parametrize("status", [301, 401, 403, 404, 500])
def test_kudu_errors_do_not_expose_response_body(kudu, status):
    _, _, responses = kudu
    responses["/api/vfs/site/wwwroot/"] = httpx.Response(status, text="private response")
    with pytest.raises(sources.CodebaseAccessError) as error:
        with sources.open_source("azure") as source:
            source.list_directory("")
    assert "private response" not in str(error.value)


@pytest.mark.parametrize("root", ["site", "../site/wwwroot", "https://host/site/wwwroot", "site/wwwroot/../private"])
def test_kudu_invalid_root_never_connects(kudu, monkeypatch, root):
    requests, _, _ = kudu
    monkeypatch.setattr(sources.settings, "AZURE_CODEBASE_PATH", root)
    with pytest.raises(sources.CodebaseAccessError):
        with sources.open_source("azure"):
            pass
    assert not requests


def test_kudu_untrusted_listing_and_size_limits(kudu, monkeypatch):
    _, _, responses = kudu
    responses["/api/vfs/site/wwwroot/"] = httpx.Response(200, json=[{"name": "../private.py"}])
    with sources.open_source("azure") as source:
        with pytest.raises(sources.CodebaseAccessError):
            source.list_directory("")
    responses["/api/vfs/site/wwwroot/worker.py"] = httpx.Response(200, content=b"oversized")
    monkeypatch.setattr(sources, "MAX_FILE_BYTES", 1)
    with sources.open_source("azure") as source:
        with pytest.raises(sources.CodebaseAccessError):
            source.read_file("worker.py")


@pytest.fixture
def sftp(monkeypatch):
    for name, value in {
        "GCP_CODEBASE_PATH": "/srv/application",
        "GCP_CODEBASE_SSH_HOST": "configured-host",
        "GCP_CODEBASE_SSH_USER": "reader",
        "GCP_CODEBASE_SSH_KEY_FILE": "configured-key",
        "GCP_CODEBASE_KNOWN_HOSTS": "configured-hosts",
    }.items():
        monkeypatch.setattr(sources.settings, name, value)
    calls = []
    modes = {
        "/srv": stat.S_IFDIR, "/srv/application": stat.S_IFDIR,
        "/srv/application/worker.py": stat.S_IFREG,
    }

    class SFTP:
        def get_channel(self):
            return self

        def settimeout(self, timeout):
            assert timeout > 0

        def lstat(self, path):
            calls.append(("lstat", path))
            return SimpleNamespace(st_mode=modes[path])

        def listdir_iter(self, path, read_aheads):
            return iter([SimpleNamespace(filename="worker.py", st_mode=stat.S_IFREG, st_size=8)])

        def open(self, path, mode):
            calls.append(("open", path, mode))
            return io.BytesIO(b"print(1)")

        def close(self):
            calls.append(("close_sftp",))

    class SSH:
        def load_host_keys(self, path):
            calls.append(("known_hosts", path))

        def set_missing_host_key_policy(self, policy):
            assert isinstance(policy, RejectPolicy)

        def connect(self, **kwargs):
            calls.append(("connect", kwargs))

        def open_sftp(self):
            return SFTP()

        def close(self):
            calls.append(("close_ssh",))

    class RejectPolicy:
        pass

    class SSHException(Exception):
        pass

    monkeypatch.setitem(sys.modules, "paramiko", SimpleNamespace(
        SSHClient=SSH, RejectPolicy=RejectPolicy, SSHException=SSHException,
    ))
    return calls, modes


def test_sftp_key_auth_host_verification_and_read_only(sftp):
    calls, _ = sftp
    with sources.open_source("gcp") as source:
        assert source.list_directory("")[0].name == "worker.py"
        assert source.read_file("worker.py") == b"print(1)"
    connect = next(call[1] for call in calls if call[0] == "connect")
    assert connect["allow_agent"] is False
    assert connect["look_for_keys"] is False
    assert connect["hostname"] == "configured-host"
    assert ("open", "/srv/application/worker.py", "rb") in calls
    assert ("close_sftp",) in calls and ("close_ssh",) in calls


def test_sftp_rejects_linked_ancestors(sftp):
    calls, modes = sftp
    modes["/srv"] = stat.S_IFLNK
    with pytest.raises(sources.CodebaseAccessError):
        with sources.open_source("gcp"):
            pass
    # Even constructor failures must close both connections.
    assert ("close_sftp",) in calls and ("close_ssh",) in calls


def test_sftp_rejects_linked_files(sftp):
    calls, modes = sftp
    modes["/srv/application/worker.py"] = stat.S_IFLNK
    with sources.open_source("gcp") as source:
        with pytest.raises(sources.CodebaseAccessError):
            source.read_file("worker.py")
    assert not any(call[0] == "open" for call in calls)
