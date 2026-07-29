#!/usr/bin/env python3
import base64
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import uuid


MAX_ASSET_BYTES = 2 * 1024 * 1024 * 1024 - 1
RELEASE_PREFIX = "lapastorage-"
TRUSTED_DOWNLOAD_PREFIX = "/FrostyBeearDevelopment/Storage/releases/download/"


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def trusted_download_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or not parsed.path.startswith(TRUSTED_DOWNLOAD_PREFIX)
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("archive inputs must be immutable Storage release URLs")
    return value


def safe_archive_path(value: str) -> str:
    if (
        not value.startswith("game/")
        or "\\" in value
        or value.startswith("/")
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise RuntimeError(f"unsafe archive path: {value!r}")
    return value


def download(url: str, destination: pathlib.Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "LapaStorage-archive-composer"})
    with urllib.request.urlopen(request, timeout=600) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)


def sha256_base64(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


def github_json(method: str, url: str, body=None, content_type="application/json"):
    token = required("GH_TOKEN")
    data = None if body is None else (
        json.dumps(body).encode("utf-8") if content_type == "application/json" else body
    )
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
            "User-Agent": "LapaStorage-archive-composer",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise RuntimeError(f"GitHub returned {error.code}: {detail}") from error


def github_upload(url: str, archive: pathlib.Path) -> dict:
    completed = subprocess.run(
        [
            "curl",
            "--fail-with-body",
            "--silent",
            "--show-error",
            "--request",
            "POST",
            "--header",
            "Accept: application/vnd.github+json",
            "--header",
            f"Authorization: Bearer {required('GH_TOKEN')}",
            "--header",
            "Content-Type: application/zip",
            "--header",
            "X-GitHub-Api-Version: 2022-11-28",
            "--data-binary",
            f"@{archive}",
            url,
        ],
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"GitHub asset upload failed: {completed.stderr or completed.stdout}")
    return json.loads(completed.stdout)


def select_release(repository: str) -> dict:
    api = f"https://api.github.com/repos/{repository}"
    releases = github_json("GET", f"{api}/releases?per_page=100")
    for release in releases:
        if release["tag_name"].startswith(RELEASE_PREFIX) and len(release.get("assets", [])) < 999:
            return release
    indexes = []
    for release in releases:
        match = re.fullmatch(rf"{re.escape(RELEASE_PREFIX)}(\d+)", release["tag_name"])
        if match:
            indexes.append(int(match.group(1)))
    tag = f"{RELEASE_PREFIX}{max(indexes, default=0) + 1:06d}"
    return github_json(
        "POST",
        f"{api}/releases",
        {"tag_name": tag, "name": tag, "draft": False, "prerelease": False},
    )


def main() -> None:
    request_id = str(uuid.UUID(required("REQUEST_ID")))
    file_name = required("FILE_NAME")
    if "/" in file_name or "\\" in file_name:
        raise RuntimeError("FILE_NAME must be a basename")
    spec = json.loads(base64.b64decode(required("PATCH_SPEC_BASE64"), validate=True))
    if spec.get("schema_version") != 1:
        raise RuntimeError("unsupported patch specification")
    replacements = spec.get("replace", [])
    removals = spec.get("remove", [])
    if not isinstance(replacements, list) or not isinstance(removals, list):
        raise RuntimeError("invalid patch specification")

    work = pathlib.Path("work")
    patch_root = work / "patch"
    patch_root.mkdir(parents=True, exist_ok=False)
    archive = (work / "archive.zip").resolve()
    print("Downloading previous full-install archive", flush=True)
    download(trusted_download_url(required("BASE_ARCHIVE_URL")), archive)

    replacement_paths = []
    for item in replacements:
        path = safe_archive_path(item["path"])
        destination = patch_root / pathlib.PurePosixPath(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        download(trusted_download_url(item["download_url"]), destination)
        if destination.stat().st_size != int(item["size"]):
            raise RuntimeError(f"downloaded size mismatch for {path}")
        if sha256_base64(destination) != item["sha256"]:
            raise RuntimeError(f"downloaded digest mismatch for {path}")
        replacement_paths.append(path)

    delete_paths = sorted(set(safe_archive_path(path) for path in removals + replacement_paths))
    if delete_paths:
        subprocess.run(
            ["zip", "-q", "-d", str(archive), *delete_paths],
            check=False,
            stdout=subprocess.DEVNULL,
        )
    if replacement_paths:
        subprocess.run(
            ["zip", "-q", "-0", str(archive), *replacement_paths],
            cwd=patch_root,
            check=True,
        )

    listing = subprocess.run(
        ["unzip", "-Z1", str(archive)],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.splitlines()
    names = set(listing)
    if len(names) != len(listing):
        raise RuntimeError("composed archive contains duplicate paths")
    for path in replacement_paths:
        if path not in names:
            raise RuntimeError(f"replacement is missing from archive: {path}")
    for path in removals:
        if path in names:
            raise RuntimeError(f"removed path remains in archive: {path}")

    size = archive.stat().st_size
    if size < 1 or size > MAX_ASSET_BYTES:
        raise RuntimeError("composed archive is outside GitHub asset size limits")
    digest = sha256_base64(archive)
    release = select_release(required("REPOSITORY"))
    asset_name = f"{uuid.uuid4()}.zip"
    upload_url = (
        f"https://uploads.github.com/repos/{required('REPOSITORY')}"
        f"/releases/{release['id']}/assets?name={urllib.parse.quote(asset_name)}"
    )
    print(f"Uploading {size} bytes to GitHub release {release['tag_name']}", flush=True)
    asset = github_upload(upload_url, archive)

    if int(asset["size"]) != size:
        raise RuntimeError("GitHub returned mismatched artifact size")
    result = {
        "file_name": file_name,
        "content_type": "application/zip",
        "sha256": digest,
        "size": size,
        "provider_container_id": str(release["id"]),
        "provider_object_id": str(asset["id"]),
        "provider_object_name": asset["name"],
        "download_url": asset["browser_download_url"],
    }
    pathlib.Path("result.json").write_text(
        json.dumps({"request_id": request_id, "provider_asset": result}, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
