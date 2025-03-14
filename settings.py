import json
import os

def load_settings():
    """Load settings from a JSON file."""
    settings_file = "settings.json"
    if os.path.exists(settings_file):
        with open(settings_file, "r") as f:
            return json.load(f)
    else:
        return create_default_settings_file()  # Create default settings if the file doesn't exist

def create_default_settings_file():
    """Create a default settings file."""
    default_settings = {
        "resolution": "1920x1080",
        "fps": 60,
        "encoder": "libx264",
        "preset": "ultrafast",
        "hotkey": "F5",
        "audio_device": "audio-default",
        "replay_buffer": False
    }
    save_settings(default_settings)
    return default_settings

def save_settings(settings):
    """Save settings to a JSON file."""
    with open("settings.json", "w") as f:
        json.dump(settings, f, indent=4)
    print("Settings saved!")
