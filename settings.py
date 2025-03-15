import json
import os

def load_settings():
    """Load settings from a JSON file."""
    settings_file = "settings.json"
    
    # Check if the settings file exists
    if os.path.exists(settings_file):
        # If it exists, load the settings from the file
        with open(settings_file, "r") as f:
            settings = json.load(f)

            # Ensure that preset and encoder are always strings (not lists)
            settings["preset"] = str(settings.get("preset", "ultrafast"))  # Default to "ultrafast"
            settings["encoder"] = str(settings.get("encoder", "libx264"))  # Default to "libx264"

            return settings
    else:
        # If not, just return an empty dictionary or log an error
        print("Settings file not found. Using default settings.")
        return {
            "resolution": "1920x1080",
            "fps": 60,
            "encoder": "libx264",
            "preset": "ultrafast",
            "hotkey": "F5",
            "audio_device": "audio-default",
            "replay_buffer": False
        }

def save_settings(settings):
    """Save settings to a JSON file."""
    with open("settings.json", "w") as f:
        json.dump(settings, f, indent=4)
    print("Settings saved!")