import base64
import io
import os
import tarfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx


class GitHubApiError(RuntimeError):
    pass


def repository_name(repo_url: str) -> str:
    parsed = urlparse(repo_url)
    if parsed.scheme != "https" or parsed.netloc != "github.com":
        raise GitHubApiError("repo must be an https://github.com/ URL")
    parts = parsed.path.strip("/").removesuffix(".git").split("/")
    if len(parts) != 2 or not all(parts):
        raise GitHubApiError("repo URL must contain an owner and repository")
    return f"{parts[0]}/{parts[1]}"


def github_token() -> str:
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        return token
    raise GitHubApiError(
        "Set GITHUB_TOKEN or GH_TOKEN to a GitHub token with contents write access"
    )


class GitHubGitApi:
    def __init__(self, repo_url: str, token: str | None = None):
        self.repository = repository_name(repo_url)
        self.base_path = f"/repos/{self.repository}"
        self.client = httpx.Client(
            base_url="https://api.github.com",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token or github_token()}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
            follow_redirects=True,
        )

    def close(self) -> None:
        self.client.close()

    def get_ref(self, branch: str) -> str | None:
        response = self.client.get(
            f"{self.base_path}/git/ref/heads/{quote(branch, safe='')}"
        )
        if response.status_code == 404:
            return None
        data = self._json(response)
        return data["object"]["sha"]

    def default_branch(self) -> str:
        return self._json(self.client.get(self.base_path))["default_branch"]

    def commit_message(self, commit_sha: str) -> str:
        data = self._json(
            self.client.get(f"{self.base_path}/git/commits/{commit_sha}")
        )
        return str(data["message"])

    def has_refs(self) -> bool:
        response = self.client.get(f"{self.base_path}/git/refs")
        if response.status_code in {404, 409}:
            return False
        response.raise_for_status()
        return bool(response.json())

    def download_ref(self, branch: str, workspace: Path) -> str | None:
        commit_sha = self.get_ref(branch)
        workspace.mkdir(parents=True, exist_ok=True)
        if commit_sha is None:
            if self.has_refs():
                raise GitHubApiError(f"startingRef does not exist: {branch}")
            return None

        response = self.client.get(
            f"{self.base_path}/tarball/{quote(commit_sha, safe='')}"
        )
        if not response.is_success:
            self._json(response)
        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
            for member in archive.getmembers():
                parts = Path(member.name).parts[1:]
                if not parts:
                    continue
                if (
                    member.issym()
                    or member.islnk()
                    or ".." in parts
                    or Path(*parts).is_absolute()
                ):
                    raise GitHubApiError("unsafe path in repository archive")
                destination = workspace.joinpath(*parts)
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise GitHubApiError("unable to read repository archive")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read())
                destination.chmod(member.mode & 0o777)
        return commit_sha

    def create_commit(
        self,
        workspace: Path,
        message: str,
        parent_sha: str | None,
    ) -> str:
        entries = []
        total_bytes = 0
        for path in self._workspace_files(workspace):
            if path.is_symlink():
                raise GitHubApiError(
                    f"symlinks are not supported by the prototype: {path}"
                )
            content = path.read_bytes()
            total_bytes += len(content)
            if total_bytes > 10_000_000:
                raise GitHubApiError("workspace exceeds the 10 MB prototype limit")
            blob = self._json(
                self.client.post(
                    f"{self.base_path}/git/blobs",
                    json={
                        "content": base64.b64encode(content).decode("ascii"),
                        "encoding": "base64",
                    },
                )
            )
            mode = "100755" if path.stat().st_mode & 0o111 else "100644"
            entries.append(
                {
                    "path": path.relative_to(workspace).as_posix(),
                    "mode": mode,
                    "type": "blob",
                    "sha": blob["sha"],
                }
            )

        tree = self._json(
            self.client.post(
                f"{self.base_path}/git/trees", json={"tree": entries}
            )
        )
        payload: dict[str, Any] = {
            "message": message,
            "tree": tree["sha"],
            "parents": [parent_sha] if parent_sha else [],
        }
        commit = self._json(
            self.client.post(f"{self.base_path}/git/commits", json=payload)
        )
        return commit["sha"]

    def write_ref(
        self,
        branch: str,
        commit_sha: str,
        existing_sha: str | None,
    ) -> None:
        if existing_sha:
            response = self.client.patch(
                f"{self.base_path}/git/refs/heads/{quote(branch, safe='')}",
                json={"sha": commit_sha, "force": False},
            )
        else:
            response = self.client.post(
                f"{self.base_path}/git/refs",
                json={"ref": f"refs/heads/{branch}", "sha": commit_sha},
            )
        self._json(response)

    def ensure_pull_request(
        self,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> dict[str, Any]:
        owner = self.repository.split("/", 1)[0]
        response = self.client.get(
            f"{self.base_path}/pulls",
            params={
                "state": "all",
                "head": f"{owner}:{head}",
                "base": base,
            },
        )
        if not response.is_success:
            self._json(response)
        existing = response.json()
        if existing:
            pull_request = existing[0]
        else:
            pull_request = self._json(
                self.client.post(
                    f"{self.base_path}/pulls",
                    json={
                        "title": title[:256],
                        "head": head,
                        "base": base,
                        "body": body,
                    },
                )
            )
        return {
            "number": pull_request["number"],
            "url": pull_request["html_url"],
        }

    @staticmethod
    def _workspace_files(workspace: Path) -> list[Path]:
        return sorted(
            path
            for path in workspace.rglob("*")
            if path.is_file() and ".git" not in path.parts
        )

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        if response.is_success:
            return response.json()
        try:
            detail = response.json().get("message", response.text)
        except ValueError:
            detail = response.text
        raise GitHubApiError(
            f"GitHub API {response.status_code}: {detail}"
        )
