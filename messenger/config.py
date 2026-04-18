import json
from pathlib import Path


def load_json_file(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def merge_dicts(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(base_config_path: Path, local_config_path: Path) -> dict:
    if not base_config_path.exists():
        raise FileNotFoundError(f"Missing base config file: {base_config_path}.")

    cfg = load_json_file(base_config_path)
    if local_config_path.exists():
        local_cfg = load_json_file(local_config_path)
        cfg = merge_dicts(cfg, local_cfg)
    else:
        print(f"[warn] local config not found: {local_config_path}")
        print("[warn] continuing with base config only")

    required_top_level = ["sender_id", "listen", "tor_socks", "peers"]
    for key in required_top_level:
        if key not in cfg:
            raise ValueError(f"Config is missing required key: {key}")

    cfg.setdefault("webrtc", {})
    cfg["webrtc"].setdefault("ice_servers", [{"urls": ["stun:stun.l.google.com:19302"]}])
    cfg["webrtc"].setdefault("signaling_timeout_sec", 30)
    cfg["webrtc"].setdefault("download_dir", "downloads")
    cfg["webrtc"].setdefault("file_chunk_bytes", 65536)

    cfg.setdefault("tor_process", {})
    cfg["tor_process"].setdefault("autostart", True)
    cfg["tor_process"].setdefault("executable", "tor/tor/tor.exe")
    cfg["tor_process"].setdefault("config", "tor/torrc")
    cfg["tor_process"].setdefault("startup_timeout_ms", 8000)

    return cfg
