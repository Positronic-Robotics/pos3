"""Named S3 profiles: registry, resolution, and isolated client creation."""

from __future__ import annotations

import configparser
import os
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import boto3
from botocore import UNSIGNED
from botocore.config import Config


@dataclass(frozen=True)
class Profile:
    """Configuration for an S3-compatible endpoint.

    Attributes:
        local_name: Identifier used in cache path (e.g., 'nebius'). Cannot be '_' (reserved).
        endpoint: S3 endpoint URL (e.g., 'https://storage.eu-north1.nebius.cloud').
        public: If True, use anonymous access (no credentials required).
        region: Optional AWS region name.
        access_key: Optional explicit AWS access key id. When set together with
            secret_key, the profile builds its own isolated boto3.Session that
            never reads or mutates the ambient AWS configuration.
        secret_key: Optional explicit AWS secret access key.
        session_token: Optional explicit AWS session token.
    """

    local_name: str
    endpoint: str
    public: bool = False
    region: str | None = None
    access_key: str | None = field(default=None, repr=False)
    secret_key: str | None = field(default=None, repr=False)
    session_token: str | None = field(default=None, repr=False)

    def __post_init__(self):
        if self.local_name == "_":
            raise ValueError("Profile local_name cannot be '_' (reserved for default)")
        if not self.local_name or not all(c.isalnum() or c in "-_" for c in self.local_name):
            raise ValueError(f"Invalid local_name '{self.local_name}': use only alphanumeric, dash, underscore")
        # access_key and secret_key are an AWS pair; one without the other
        # would silently fall back to the ambient credential chain in
        # _create_s3_client and route operations to the wrong account.
        if bool(self.access_key) != bool(self.secret_key):
            raise ValueError(
                "Profile access_key and secret_key must be set together "
                "(or both omitted to use the default credential chain)."
            )


# Profiles registered programmatically via register_profile. Lookups consult
# this dict first, so a code registration unconditionally wins over a registry
# entry of the same name regardless of load order.
_PROFILES: dict[str, Profile] = {}
# Profiles loaded from the on-disk registry. Consulted only as a fallback.
_REGISTRY_PROFILES: dict[str, Profile] = {}


def register_profile(
    name: str,
    endpoint: str,
    public: bool = False,
    region: str | None = None,
    local_name: str | None = None,
) -> None:
    """Register a named profile for S3 access.

    Creates a Profile with the given parameters. See Profile class for field details.
    The `local_name` defaults to the profile `name` if not specified. A code
    registration always takes precedence over a registry-file entry of the same
    name; re-registering the same name with a different config raises.
    """
    config = Profile(local_name=local_name or name, endpoint=endpoint, public=public, region=region)
    existing = _PROFILES.get(name)
    if existing is not None and existing != config:
        raise ValueError(f"Profile '{name}' already registered with different config")
    _PROFILES[name] = config


_REGISTRY_LOADED = False
_REGISTRY_LOCK = threading.Lock()


def _default_profiles_path() -> Path:
    """Resolve the location of the auto-loaded profile registry file.

    Honors POS3_PROFILES_FILE, then XDG_CONFIG_HOME, defaulting to
    ~/.config/pos3/profiles.toml.
    """
    override = os.environ.get("POS3_PROFILES_FILE")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "pos3" / "profiles.toml"


def _load_credentials_file(creds_path: Path, profile_name: str) -> tuple[str, str, str | None]:
    """Read AWS-style credentials for a profile from a separate secret file.

    The file uses INI sections (like ~/.aws/credentials). Only the section
    named after the profile or '[default]' is accepted; anything else is a
    hard error. Both `aws_access_key_id` and `aws_secret_access_key` are
    required. These guards exist because any silent fallback (to an
    arbitrary section, or to the ambient AWS credential chain) could route
    operations to the wrong account, defeating the point of isolated
    per-profile credentials.
    """
    parser = configparser.ConfigParser()
    if not parser.read(creds_path):
        raise ValueError(f"Profile credentials_file not found or unreadable: {creds_path}")

    if parser.has_section(profile_name):
        section = profile_name
    elif parser.has_section("default"):
        section = "default"
    else:
        # Don't fall back to an arbitrary section: a typo in the profile-name
        # header would otherwise silently bind credentials from an unrelated
        # account, defeating the point of isolated per-profile credentials.
        raise ValueError(
            f"Credentials file {creds_path} must contain a [{profile_name}] "
            f"or [default] section; found sections: {parser.sections()}"
        )

    sec = parser[section]
    access_key = sec.get("aws_access_key_id")
    secret_key = sec.get("aws_secret_access_key")
    if not access_key or not secret_key:
        raise ValueError(
            f"Credentials section '{section}' in {creds_path} must define both "
            "'aws_access_key_id' and 'aws_secret_access_key'."
        )
    return access_key, secret_key, sec.get("aws_session_token")


def _profile_from_config(name: str, cfg: dict[str, Any], source: Path) -> Profile:
    """Build a Profile from one [profiles.<name>] table of the registry file."""
    if not isinstance(cfg, dict):
        raise ValueError(f"Profile '{name}' in {source} must be a table")
    endpoint = cfg.get("endpoint")
    if not endpoint:
        raise ValueError(f"Profile '{name}' in {source} is missing required 'endpoint'")

    access_key = secret_key = session_token = None
    creds_file = cfg.get("credentials_file")
    if creds_file:
        # Resolve relative paths against the registry file's directory, not
        # CWD, so a profiles.toml co-located with its .creds files works
        # regardless of where pos3 is invoked from.
        creds_path = Path(creds_file).expanduser()
        if not creds_path.is_absolute():
            creds_path = source.parent / creds_path
        access_key, secret_key, session_token = _load_credentials_file(creds_path, name)

    return Profile(
        local_name=cfg.get("local_name", name),
        endpoint=endpoint,
        public=cfg.get("public", False),
        region=cfg.get("region"),
        access_key=access_key,
        secret_key=secret_key,
        session_token=session_token,
    )


def _load_profile_registry(path: Path | None = None, force: bool = False) -> None:
    """Auto-load named profiles from the local registry file (once per process).

    Registry entries are kept in a separate dict from programmatic registrations,
    so a code register_profile() always wins over a registry entry of the same
    name regardless of which happened first.
    """
    global _REGISTRY_LOADED
    with _REGISTRY_LOCK:
        if _REGISTRY_LOADED and not force:
            return
        registry_path = path if path is not None else _default_profiles_path()
        if registry_path.exists():
            with open(registry_path, "rb") as fh:
                data = tomllib.load(fh)
            # Build all profiles first so a malformed entry doesn't leave the
            # registry half-loaded with a sticky _REGISTRY_LOADED flag.
            new_profiles = {
                name: _profile_from_config(name, cfg, registry_path)
                for name, cfg in data.get("profiles", {}).items()
            }
            _REGISTRY_PROFILES.update(new_profiles)
        _REGISTRY_LOADED = True


def _resolve_profile(profile: str | Profile | None) -> Profile | None:
    """Resolve a profile name to a Profile object.

    Args:
        profile: None, registered profile name (string), or Profile object.

    Returns:
        Profile object or None.

    Raises:
        ValueError: If profile is a string that is not registered.
    """
    if profile is None or isinstance(profile, Profile):
        return profile
    # Code registrations always win; only consult the on-disk registry as a fallback.
    if profile in _PROFILES:
        return _PROFILES[profile]
    if profile not in _REGISTRY_PROFILES:
        _load_profile_registry()
    if profile in _PROFILES:
        return _PROFILES[profile]
    if profile in _REGISTRY_PROFILES:
        return _REGISTRY_PROFILES[profile]
    raise ValueError(
        f"Unknown profile: '{profile}'. Register with pos3.register_profile() "
        f"or define it in {_default_profiles_path()}."
    )


def _url_profile(s3_url: str) -> str | None:
    """Extract an explicit profile name from the userinfo slot of an S3 URL.

    `s3://<profile>@bucket/key` selects `<profile>`. No userinfo -> None.
    An empty selector (`s3://@bucket/key`, `s3://:token@bucket/key`) is a
    hard error: a templated CLI variable that expanded to nothing must not
    silently fall back to the argument/default profile.
    """
    parsed = urlparse(s3_url)
    if parsed.scheme != "s3":
        return None
    if "@" not in parsed.netloc:
        return None
    if not parsed.username:
        raise ValueError(
            f"Empty profile selector in S3 URL: {s3_url!r}. "
            "Use s3://<profile>@bucket/key or omit the '@'."
        )
    return parsed.username


def _create_s3_client(profile: Profile | None = None):
    """Create boto3 S3 client, optionally using a profile.

    Args:
        profile: None (use boto3 defaults) or Profile config.
    """
    if profile is None:
        return boto3.client("s3")

    kwargs: dict[str, Any] = {"endpoint_url": profile.endpoint}

    if profile.public:
        kwargs["config"] = Config(signature_version=UNSIGNED)

    if profile.access_key and profile.secret_key:
        # Isolated session: explicit credentials, never reads or mutates the
        # user's ambient AWS configuration in either direction.
        session = boto3.session.Session(
            aws_access_key_id=profile.access_key,
            aws_secret_access_key=profile.secret_key,
            aws_session_token=profile.session_token,
            region_name=profile.region,
        )
        return session.client("s3", **kwargs)

    if profile.region:
        kwargs["region_name"] = profile.region

    return boto3.client("s3", **kwargs)
