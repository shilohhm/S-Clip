import sys
from PyQt5.QtWidgets import QApplication
from gui import ClippingApp
from hotkey import HotkeyListener

if __name__ == "__main__":
    app = QApplication(sys.argv)  # Create QApplication first
    window = ClippingApp()        # Create GUI after QApplication
    window.show()

    # Start the hotkey listener with reference to the GUI
    hotkey_listener = HotkeyListener(window)
    hotkey_listener.start()

    sys.exit(app.exec_())


