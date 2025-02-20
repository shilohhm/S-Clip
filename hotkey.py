from pynput import keyboard

# Mapping from string to pynput keyboard.Key
KEY_MAP = {
    "F1": keyboard.Key.f1,
    "F2": keyboard.Key.f2,
    "F3": keyboard.Key.f3,
    "F4": keyboard.Key.f4,
    "F5": keyboard.Key.f5,
    "F6": keyboard.Key.f6,
    "F7": keyboard.Key.f7,
    "F8": keyboard.Key.f8,
    "F9": keyboard.Key.f9,
    "F10": keyboard.Key.f10,
    "F11": keyboard.Key.f11,
    "F12": keyboard.Key.f12,
}

class HotkeyListener:
    def __init__(self, gui_instance):
        self.gui_instance = gui_instance
        self.listener = keyboard.Listener(on_press=self.on_press)

    def start(self):
        self.listener.start()

    def on_press(self, key):
        desired_key_str = self.gui_instance.settings.get("hotkey", "F5").upper()
        desired_key = KEY_MAP.get(desired_key_str, keyboard.Key.f5)
        if key == desired_key:
            print(f"{desired_key_str} pressed! Clipping the last 30 seconds...")
            self.gui_instance.clip_last_30_seconds()
