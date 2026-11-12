from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import tomllib
import unicodedata

from sglance.models import ClusterEntry

_ALLOWED_CLUSTER_KEYS = frozenset({"name", "ssh", "local", "slurm_cluster"})


class ConfigError(Exception):
    """Raised when configuration cannot be found, parsed, or validated."""


def validate_safe_token(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{field_name} must be a non-empty string")
    if value.startswith("-"):
        raise ConfigError(f"{field_name} {value!r} must not start with '-'")
    if "," in value:
        raise ConfigError(f"{field_name} {value!r} must not contain commas")
    if any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in value):
        raise ConfigError(f"{field_name} {value!r} must not contain whitespace or control characters")
    return value


def load_config(env: Mapping[str, str]) -> tuple[ClusterEntry, ...]:
    xdg_config = env.get("XDG_CONFIG_HOME", "")
    if xdg_config:
        config_base = Path(xdg_config)
    else:
        home_dir = env.get("HOME", "")
        if not home_dir:
            raise ConfigError("neither XDG_CONFIG_HOME nor HOME is set to locate config.toml")
        config_base = Path(home_dir) / ".config"

    config_path = config_base / "sglance" / "config.toml"
    try:
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file {config_path}: {exc.strerror or exc}") from exc
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"malformed configuration file {config_path}: {exc}") from exc

    unknown_top_keys = sorted(set(data) - {"clusters"})
    if unknown_top_keys:
        raise ConfigError(f"unknown top-level key(s) in {config_path}: {', '.join(unknown_top_keys)}")

    raw_clusters = data.get("clusters")
    if not isinstance(raw_clusters, list) or not raw_clusters:
        raise ConfigError(f"{config_path} must define at least one [[clusters]] entry")

    entries: list[ClusterEntry] = []
    seen_names: set[str] = set()
    for idx, item in enumerate(raw_clusters):
        if not isinstance(item, dict):
            raise ConfigError(f"clusters[{idx}] in {config_path} must be a table")
        unknown_entry_keys = sorted(set(item) - _ALLOWED_CLUSTER_KEYS)
        if unknown_entry_keys:
            raise ConfigError(
                f"unknown key(s) in clusters[{idx}] of {config_path}: {', '.join(unknown_entry_keys)}"
            )
        if "name" not in item:
            raise ConfigError(f"clusters[{idx}] in {config_path} is missing required key 'name'")
        name = validate_safe_token(item["name"], field_name=f"clusters[{idx}].name")
        if name in seen_names:
            raise ConfigError(f"duplicate cluster name {name!r} in {config_path}")
        seen_names.add(name)

        local = item.get("local", False)
        if not isinstance(local, bool):
            raise ConfigError(f"clusters[{idx}].local in {config_path} must be a boolean")

        if local and "ssh" in item:
            raise ConfigError(
                f"clusters[{idx}] ({name!r}) in {config_path} cannot set both local = true and ssh"
            )
        if local:
            ssh: str | None = None
        elif "ssh" in item:
            ssh = validate_safe_token(item["ssh"], field_name=f"clusters[{idx}].ssh")
        else:
            ssh = name

        slurm_cluster: str | None = None
        if "slurm_cluster" in item:
            slurm_cluster = validate_safe_token(
                item["slurm_cluster"], field_name=f"clusters[{idx}].slurm_cluster"
            )

        entries.append(ClusterEntry(name=name, ssh=ssh, local=local, slurm_cluster=slurm_cluster))
    return tuple(entries)
