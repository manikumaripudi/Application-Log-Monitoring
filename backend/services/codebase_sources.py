"""Read-only cloud source transports. Never follow server-provided URLs."""

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import PurePosixPath
import re
import stat
import time
from urllib.parse import quote

import httpx

from config.settings import settings

MAX_FILE_BYTES = 256 * 1024
MAX_ENTRIES = 10000
TIMEOUT = 20


class CodebaseAccessError(Exception):
    """Safe diagnostic message suitable for model output and logging."""


@dataclass(frozen=True)
class SourceEntry:
    name: str
    is_directory: bool
    size: int
    linked: bool = False


def relative_parts(path: str, allow_empty: bool = False) -> tuple:
    if allow_empty and path == "":
        return ()
    if not isinstance(path, str) or not path or any(
        char in path for char in ("\\", ":", "%", "\x00", "?", "#")
    ):
        raise CodebaseAccessError("Use a plain repository-relative path returned by scan_codebase.")
    parts = path.split("/")
    if any(part in ("", ".", "..") or any(ord(c) < 32 for c in part) for part in parts):
        raise CodebaseAccessError("Absolute paths and traversal are not allowed.")
    return tuple(parts)


class RemoteSource:
    def __init__(self):
        self.deadline = time.monotonic() + TIMEOUT
        self.requests = 0

    def check(self, request: bool = True):
        if request:
            self.requests += 1
        if self.requests > 100 or time.monotonic() >= self.deadline:
            raise CodebaseAccessError("Cloud inspection time or request limit reached; results are incomplete.")

    def remaining(self):
        return max(0.1, self.deadline - time.monotonic())


class KuduSource(RemoteSource):
    def __init__(self):
        super().__init__()
        app = settings.AZURE_APP_SERVICE_NAME or ""
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,58}[A-Za-z0-9])?", app):
            raise CodebaseAccessError("Configure a valid AZURE_APP_SERVICE_NAME for Kudu inspection.")
        if not settings.KUDU_USERNAME or not settings.KUDU_PASSWORD:
            raise CodebaseAccessError("Kudu inspection requires KUDU_USERNAME and KUDU_PASSWORD.")
        root = settings.AZURE_CODEBASE_PATH or ""
        # Kudu VFS is rooted at the App Service home directory, on Windows and Linux.
        if re.match(r"^[A-Za-z]:\\home\\", root, re.IGNORECASE):
            root = root[8:].replace("\\", "/")
        elif root.startswith("/home/"):
            root = root[len("/home/"):]
        parts = relative_parts(root.rstrip("/"))
        if len(parts) < 2 or parts[:2] != ("site", "wwwroot"):
            raise CodebaseAccessError("AZURE_CODEBASE_PATH must be site/wwwroot or a subdirectory.")
        self.base_url = f"https://{app}.scm.azurewebsites.net/api/vfs/" + "/".join(
            quote(part, safe="") for part in parts
        ) + "/"
        self.client = httpx.Client(
            auth=(settings.KUDU_USERNAME, settings.KUDU_PASSWORD),
            follow_redirects=False, timeout=TIMEOUT, trust_env=False,
        )

    def close(self):
        self.client.close()

    def _get(self, relative: str, directory: bool, limit: int) -> bytes:
        self.check(request=False)
        parts = relative_parts(relative, allow_empty=directory)
        url = self.base_url + "/".join(quote(part, safe="") for part in parts)
        if directory and not url.endswith("/"):
            url += "/"
        with self.client.stream("GET", url, timeout=self.remaining()) as response:
            if response.status_code != 200:
                raise CodebaseAccessError(
                    f"Kudu read failed (HTTP {response.status_code}); check access, path and credentials."
                )
            content = bytearray()
            for chunk in response.iter_bytes(chunk_size=8192):
                self.check()
                content.extend(chunk)
                if len(content) > limit:
                    raise CodebaseAccessError("Kudu response exceeds the inspection size limit.")
            return bytes(content)

    def list_directory(self, relative: str) -> list:
        try:
            entries = json.loads(self._get(relative, True, 1024 * 1024))
            if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
                raise ValueError
            result = []
            for entry in entries:
                name = entry["name"]
                if len(relative_parts(name)) != 1:
                    raise ValueError
                size = entry.get("size", 0)
                if type(size) is not int or size < 0:
                    raise ValueError
                result.append(SourceEntry(
                    name, entry.get("mime") == "inode/directory", size,
                    entry.get("mime") == "inode/symlink",
                ))
            return result
        except (ValueError, KeyError, TypeError):
            raise CodebaseAccessError("Kudu returned an invalid directory listing.") from None

    def read_file(self, relative: str) -> bytes:
        return self._get(relative, False, MAX_FILE_BYTES)


class SftpSource(RemoteSource):
    def __init__(self):
        super().__init__()
        required = (
            settings.GCP_CODEBASE_PATH, settings.GCP_CODEBASE_SSH_HOST,
            settings.GCP_CODEBASE_SSH_USER, settings.GCP_CODEBASE_SSH_KEY_FILE,
            settings.GCP_CODEBASE_KNOWN_HOSTS,
        )
        if not all(required):
            raise CodebaseAccessError("Configure the GCP codebase path, SSH host/user/key and known-hosts file.")
        root = settings.GCP_CODEBASE_PATH
        if not root.startswith("/") or root == "/":
            raise CodebaseAccessError("GCP_CODEBASE_PATH must be an absolute dedicated application directory.")
        relative_parts(root[1:].rstrip("/"))
        if not 1 <= settings.GCP_CODEBASE_SSH_PORT <= 65535:
            raise CodebaseAccessError("GCP codebase SSH port is invalid.")
        try:
            import paramiko
        except ImportError:
            raise CodebaseAccessError("Install backend requirements to enable GCP SFTP inspection.") from None
        self.ssh = paramiko.SSHClient()
        self.sftp = None
        try:
            self.ssh.load_host_keys(settings.GCP_CODEBASE_KNOWN_HOSTS)
            self.ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
            self.ssh.connect(
                hostname=settings.GCP_CODEBASE_SSH_HOST,
                port=settings.GCP_CODEBASE_SSH_PORT,
                username=settings.GCP_CODEBASE_SSH_USER,
                key_filename=settings.GCP_CODEBASE_SSH_KEY_FILE,
                passphrase=settings.GCP_CODEBASE_SSH_KEY_PASSPHRASE,
                look_for_keys=False, allow_agent=False, timeout=self.remaining(),
                auth_timeout=self.remaining(), banner_timeout=self.remaining(),
            )
            self.sftp = self.ssh.open_sftp()
            self.sftp.get_channel().settimeout(self.remaining())
            self.root = root.rstrip("/")
            self._path("")
        except CodebaseAccessError:
            self.close()
            raise
        except (paramiko.SSHException, OSError, ValueError, EOFError):
            self.close()
            raise CodebaseAccessError("SFTP connection failed; check host key, key authentication, network and path.") from None

    def close(self):
        try:
            if self.sftp is not None:
                self.sftp.close()
        finally:
            self.ssh.close()

    def _path(self, relative: str) -> str:
        parts = relative_parts(relative, allow_empty=True)
        # Check every ancestor, including the configured root, without following links.
        path = ""
        for part in PurePosixPath(self.root).parts[1:] + parts:
            self.check()
            self.sftp.get_channel().settimeout(self.remaining())
            path += "/" + part
            mode = self.sftp.lstat(path).st_mode
            if mode is None or stat.S_ISLNK(mode):
                raise CodebaseAccessError("SFTP symlinks and unknown file types are not permitted.")
        return path

    def list_directory(self, relative: str) -> list:
        path = self._path(relative)
        self.check()
        result = []
        for entry in self.sftp.listdir_iter(path, read_aheads=1):
            self.check(request=False)
            if len(result) >= MAX_ENTRIES:
                raise CodebaseAccessError("SFTP directory entry limit reached.")
            if len(relative_parts(entry.filename)) != 1:
                raise CodebaseAccessError("SFTP returned an invalid entry name.")
            mode = entry.st_mode or 0
            result.append(SourceEntry(
                entry.filename, stat.S_ISDIR(mode), entry.st_size or 0,
                not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)),
            ))
        return result

    def read_file(self, relative: str) -> bytes:
        path = self._path(relative)
        self.check()
        if not stat.S_ISREG(self.sftp.lstat(path).st_mode or 0):
            raise CodebaseAccessError("Only regular source files may be read.")
        with self.sftp.open(path, "rb") as source:
            content = source.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise CodebaseAccessError("SFTP file exceeds the inspection size limit.")
        return content


@contextmanager
def open_source(cloud: str):
    source = None
    transport_errors = (httpx.HTTPError, OSError, EOFError)
    if cloud == "gcp":
        try:
            import paramiko
        except ImportError:
            raise CodebaseAccessError("Install backend requirements to enable GCP SFTP inspection.") from None
        transport_errors += (paramiko.SSHException,)
    try:
        if cloud == "azure":
            source = KuduSource()
        elif cloud == "gcp":
            source = SftpSource()
        else:
            raise CodebaseAccessError("No cloud codebase is bound to this analysis (mock or unknown source).")
        yield source
    except transport_errors:
        raise CodebaseAccessError("Cloud code read failed; check network connectivity and read permissions.") from None
    finally:
        if source is not None:
            try:
                source.close()
            except transport_errors:
                raise CodebaseAccessError("Cloud inspection connection could not be closed cleanly.") from None
