"""Read selected local configuration values without executing or logging them."""

import os
from pathlib import Path


def credentials(env_file, key_id_env="OTHRYSS_KALSHI_KEY_ID", explicit_key_file=None):
    env_file = Path(env_file)
    wanted = {key_id_env, "OTHRYSS_KALSHI_PRIVATE_KEY_PATH"}
    values = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, separator, value = line.partition("=")
            name = name.strip()
            if name not in wanted:
                continue
            if not separator or name in values:
                raise ValueError("Invalid or duplicate credential setting in local environment file")
            value = value.strip()
            if value[:1] in {"'", '"'}:
                if len(value) < 2 or value[-1] != value[0]:
                    raise ValueError("Unbalanced quotes in credential setting")
                value = value[1:-1]
            values[name] = value
    key_id = os.environ.get(key_id_env) or values.get(key_id_env)
    key_file = explicit_key_file or os.environ.get("OTHRYSS_KALSHI_PRIVATE_KEY_PATH")
    if not key_file and values.get("OTHRYSS_KALSHI_PRIVATE_KEY_PATH"):
        path = Path(values["OTHRYSS_KALSHI_PRIVATE_KEY_PATH"])
        key_file = path if path.is_absolute() else env_file.parent / path
    if not key_id or not key_file:
        raise ValueError("Set OTHRYSS_KALSHI_KEY_ID and OTHRYSS_KALSHI_PRIVATE_KEY_PATH in local.env, or supply the documented environment/CLI settings")
    return key_id, Path(key_file)
