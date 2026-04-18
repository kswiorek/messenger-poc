from pathlib import Path

from messenger.app import MessengerApp
from messenger.config import load_config


BASE_CONFIG_PATH = Path("messenger_config.json")
LOCAL_CONFIG_PATH = Path("messenger_config.local.json")


def main() -> None:
    cfg = load_config(BASE_CONFIG_PATH, LOCAL_CONFIG_PATH)
    app = MessengerApp(cfg)
    app.start()
    try:
        app.run_cli()
    finally:
        app.stop()


if __name__ == "__main__":
    main()
