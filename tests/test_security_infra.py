"""Deployment and supply-chain hardening: Dockerfile, compose, lock files, CI, secrets."""

import asyncio
import hashlib
import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _locked(name: str) -> dict[str, str]:
    """Package name -> pinned version from a uv/pip-compile lock file."""
    pins = {}
    for line in _read(name).splitlines():
        m = re.match(r"^([A-Za-z0-9._-]+)==([^\s\\;]+)", line)
        if m:
            pins[m.group(1).lower().replace("_", "-")] = m.group(2)
    return pins


def _compose_default(var: str) -> str:
    m = re.search(rf"{var}: \$\{{{var}:-([^}}]*)\}}", _read("docker-compose.yaml"))
    assert m, f"{var} has no default in docker-compose.yaml"
    return m.group(1)


# --- F04: who may set X-Forwarded-For ---------------------------------------------------------


def test_dockerfile_does_not_trust_every_proxy():
    dockerfile = _read("Dockerfile")
    assert "--forwarded-allow-ips" not in dockerfile
    assert "FORWARDED_ALLOW_IPS=*" not in dockerfile
    assert "--proxy-headers" in dockerfile


def _client_seen(trusted: str, peer: str, xff: str | None) -> tuple[str, str]:
    seen = []

    async def app(scope, receive, send):
        seen.append((scope["client"][0], scope["scheme"]))

    headers = [(b"x-forwarded-proto", b"https")]
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    scope = {"type": "http", "client": (peer, 1234), "scheme": "http", "headers": headers}
    asyncio.run(ProxyHeadersMiddleware(app, trusted_hosts=trusted)(scope, None, None))
    return seen[0]


@pytest.mark.parametrize(
    ("peer", "xff", "client", "scheme"),
    [
        # Traefik on a Docker network appends the real client; a spoofed entry on the left is
        # ignored.
        ("10.0.1.5", "6.6.6.6, 203.0.113.7", "203.0.113.7", "https"),
        ("172.18.0.2", "198.51.100.9", "198.51.100.9", "https"),
        ("192.168.5.4", "1.1.1.1, 198.51.100.9, 10.0.0.3", "198.51.100.9", "https"),
        # A peer that is not a trusted proxy cannot set its IP or the scheme.
        ("203.0.113.50", "1.2.3.4", "203.0.113.50", "http"),
    ],
)
def test_compose_proxy_trust_takes_rightmost_untrusted_address(peer, xff, client, scheme):
    trusted = _compose_default("FORWARDED_ALLOW_IPS")
    assert "*" not in trusted
    assert _client_seen(trusted, peer, xff) == (client, scheme)


def test_dockerfile_and_compose_agree_on_trusted_proxies():
    m = re.search(r"FORWARDED_ALLOW_IPS=(\S+)", _read("Dockerfile"))
    assert m and m.group(1) == _compose_default("FORWARDED_ALLOW_IPS")


# --- F51 / F75 / F56: compose ------------------------------------------------------------------


def test_compose_has_no_default_database_password():
    compose = _read("docker-compose.yaml")
    assert re.search(r"DATABASE_URL: \$\{DATABASE_URL:\?", compose)
    assert "POSTGRES_PASSWORD:-" not in compose
    override = _read("docker-compose.override.yaml")
    assert re.search(r"POSTGRES_PASSWORD: \$\{POSTGRES_PASSWORD:\?", override)
    assert "POSTGRES_PASSWORD:-" not in override


def test_local_override_publishes_on_localhost_only():
    ports = re.findall(r'^\s+- "([^"]+:8000)"', _read("docker-compose.override.yaml"), re.M)
    assert ports == ["127.0.0.1:8000:8000"]


def test_compose_limits_and_hardening():
    compose = _read("docker-compose.yaml")
    for needle in ("mem_limit:", "memswap_limit:", "pids_limit:", "read_only: true",
                   "no-new-privileges:true", "- ALL", "/srv/data"):
        assert needle in compose, needle
    cmd = _read("Dockerfile")
    assert '"--limit-concurrency"' in cmd and '"--timeout-keep-alive"' in cmd
    assert '"--ws", "none"' in cmd


def test_dockerfile_keeps_code_read_only_for_runtime_user():
    dockerfile = _read("Dockerfile")
    assert "chown -R" not in dockerfile
    assert "chown crypie:crypie /srv/data" in dockerfile
    assert dockerfile.index("COPY app") < dockerfile.index("USER crypie")
    assert "--chown" not in dockerfile


def test_dockerignore_keeps_secrets_and_data_out_of_the_build():
    ignored = set(_read(".dockerignore").split())
    for pattern in (".git", ".env", ".env.*", "!.env.example", "data/", "*.sqlite3"):
        assert pattern in ignored, pattern


# --- F53 / F54 / F58: base image and dependencies ----------------------------------------------


def test_base_image_is_pinned_by_digest():
    m = re.search(r"^FROM (\S+)", _read("Dockerfile"), re.M)
    assert m and re.fullmatch(r"python:3\.15[\w.-]*@sha256:[0-9a-f]{64}", m.group(1))


def test_dockerfile_installs_only_hashed_locked_packages():
    dockerfile = _read("Dockerfile")
    assert "pip install --require-hashes" in dockerfile
    assert "-r requirements.txt" in dockerfile
    # No other pip install that could resolve unpinned packages.
    assert dockerfile.count("pip install") == 1


@pytest.mark.parametrize("lock", ["requirements.txt", "requirements-dev.txt"])
def test_lock_files_pin_every_package_with_hashes(lock):
    blocks = re.split(r"\n(?=[A-Za-z0-9])", _read(lock))
    pinned = [b for b in blocks if re.match(r"^[A-Za-z0-9._-]+==", b)]
    assert pinned
    for block in pinned:
        assert "--hash=sha256:" in block, block.splitlines()[0]


def test_lock_satisfies_pyproject_and_dev_lock_matches():
    project = tomllib.loads(_read("pyproject.toml"))["project"]
    prod, dev = _locked("requirements.txt"), _locked("requirements-dev.txt")
    for spec in project["dependencies"]:
        req = Requirement(spec)
        name = req.name.lower().replace("_", "-")
        assert name in prod, f"{name} missing from requirements.txt: regenerate it"
        assert req.specifier.contains(prod[name], prereleases=True), (name, prod[name])
    for spec in project["optional-dependencies"]["dev"]:
        name = Requirement(spec).name.lower()
        assert name in dev, f"{name} missing from requirements-dev.txt: regenerate it"
    # The dev lock (CI) tests exactly the versions the image runs.
    assert {k: dev[k] for k in prod} == prod


def test_no_unused_uvicorn_extras():
    prod = _locked("requirements.txt")
    for unused in ("websockets", "watchfiles", "pyyaml"):
        assert unused not in prod
    deps = tomllib.loads(_read("pyproject.toml"))["project"]["dependencies"]
    assert not any(d.startswith("uvicorn[") for d in deps)


# --- F55: CI -------------------------------------------------------------------------------------


def test_ci_actions_pinned_and_token_read_only():
    ci = _read(".github/workflows/ci.yml")
    uses = re.findall(r"uses: (\S+)", ci)
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref
    assert re.search(r"^permissions:\n  contents: read$", ci, re.M)
    checkouts = ci.count("uses: actions/checkout@")
    assert ci.count("persist-credentials: false") == checkouts


# --- F60: vendored files ------------------------------------------------------------------------


def test_vendored_files_match_sha256sums():
    lines = [
        line for line in _read("app/static/vendor/SHA256SUMS").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines
    for line in lines:
        digest, path = line.split(maxsplit=1)
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest, path
    vendored = {p.name for p in (ROOT / "app/static/vendor").iterdir()} - {"SHA256SUMS"}
    assert vendored == {Path(line.split(maxsplit=1)[1]).name for line in lines}


# --- F59: secrets from files --------------------------------------------------------------------


def test_secret_file_is_used_when_env_var_is_empty(tmp_path, monkeypatch):
    (tmp_path / "bitvavo_api_secret").write_text("from-file\n")
    monkeypatch.setenv("BITVAVO_API_SECRET", "")  # what compose passes when it is unset
    s = Settings(_env_file=None, _secrets_dir=tmp_path)
    assert s.bitvavo_api_secret == "from-file"


def test_env_var_is_used_without_secret_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BITVAVO_API_SECRET", "from-env")
    assert Settings(_env_file=None, _secrets_dir=tmp_path).bitvavo_api_secret == "from-env"


def test_secrets_dir_only_set_when_it_exists():
    expected = "/run/secrets" if Path("/run/secrets").is_dir() else None
    assert Settings.model_config.get("secrets_dir") == expected
