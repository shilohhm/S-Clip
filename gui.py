import json
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QSpinBox, QComboBox, QPushButton, QMessageBox, QSizePolicy, QCheckBox, QApplication
)
from PyQt5.QtCore import Qt, QFile, QTextStream, QPropertyAnimation, QEasingCurve
from PyQt5.QtGui import QFont, QIcon
from capture import CaptureThread
import settings

class ClippingApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("S-Clip")
        self.setWindowIcon(QIcon("sclip_icon.png"))  # Ensure your icon file is in the project folder or adjust the path.
        self.setGeometry(100, 100, 1000, 700)  # Larger window size
        
        self.settings = settings.load_settings()

        # Status label for feedback
        self.status_label = QLabel("Status")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setFont(QFont("Segoe UI", 16))
        self.status_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        


        self.load_styles()
       

        
        self.replay_buffer_checkbox = QCheckBox("Replay Buffer")
        self.replay_buffer_checkbox.stateChanged.connect(self.toggle_replay_buffer)

        
        self.replay_buffer_checkbox.setChecked(self.settings["replay_buffer"])


        self.encoder_presets = {
            "libx264": ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "veryslow"],
            "h264_nvenc": ["default", "hp","llhp","fast", "medium","slow","bq","hq","ll","llhq","lossless","losslesshp",],
            "hevc_nvenc": ["fast", "medium", "slow"]
        }
        
        self.audio_devices = {"Audio Input": CaptureThread.list_audio_devices(), "Audio Output": CaptureThread.list_audio_devices()}
        
        # Layout for main window
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setSpacing(25)  # Spacing for better fluidity
        main_layout.setContentsMargins(40, 40, 40, 40)

        


        # Title label
        title_label = QLabel("S-Clip")
        title_label.setAlignment(Qt.AlignCenter)
        title_label.setFont(QFont("Segoe UI", 28, QFont.Bold))
        title_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        main_layout.addWidget(title_label)

        # Settings Layout: Resolution, FPS, Encoder, etc.
        settings_layout = QHBoxLayout()
        settings_layout.setSpacing(30)
        main_layout.addLayout(settings_layout)

    


        # Add Widgets for Resolution, FPS, Encoder, etc.
        self.add_resolution_field(settings_layout)
        self.add_fps_field(settings_layout)
        self.add_encoder_field(settings_layout)
        self.add_preset_field(settings_layout)
        self.add_hotkey_field(settings_layout)
                
        # Set values for the fields after they're added
        self.resolution_edit.setText(self.settings["resolution"])  # Set the loaded value
        self.fps_spin.setValue(self.settings["fps"])  # Set the loaded value
        self.encoder.setCurrentText(self.settings["encoder"])  # Set the loaded value
        self.preset.setCurrentText(self.settings["preset"])  # Set the loaded value
        self.hotkey_edit.setText(self.settings["hotkey"])  # Set the loaded value


        # Buttons layout
        self.add_buttons(main_layout)

        # Audio input/output
        self.add_audio_input_output(main_layout)

        #Add replay buffer checkbox
        self.add_replay_buffer_checkbox(main_layout)

        main_layout.addWidget(self.status_label)
        main_layout.addLayout(settings_layout)

        # Monitor selection
        monitor_layout = QVBoxLayout()
        monitor_label = QLabel("Select Monitor:")
        self.monitor_combo = QComboBox()
        monitors = CaptureThread.list_monitors()

        # Populate dropdown with available monitors
        for monitor in monitors.keys():
            self.monitor_combo.addItem(monitor)

        monitor_layout.addWidget(monitor_label)
        monitor_layout.addWidget(self.monitor_combo)
        settings_layout.addLayout(monitor_layout)

        # Load saved monitor setting (default to first monitor)
        self.monitor_combo.setCurrentText(self.settings.get("monitor", "Monitor 1"))




        self.capture_thread = None

    def load_styles(self):
    # Open the QSS file and apply it to the application
        style_file = QFile("styles.qss")
        if style_file.open(QFile.ReadOnly):
            stream = QTextStream(style_file)
            self.setStyleSheet(stream.readAll())
            print("QSS file loaded")
        else:
            print("QSS file didn't load!")

    def save_settings(self):
        # Retrieve updated values from the GUI elements (QLineEdit, QComboBox, etc.)
        resolution_value = self.resolution_edit.text().strip()
        fps_value = self.fps_spin.value()
        encoder_value = self.encoder.currentText()
        preset_value = self.preset.currentText()
        hotkey_value = self.hotkey_edit.text().strip()
        audio_device_value = self.audio_input_combo.currentText()
        replay_buffer_value = self.replay_buffer_checkbox.isChecked()

        # Create a settings dictionary with updated values
        settings_dict = {
            "resolution": resolution_value,
            "fps": fps_value,
            "encoder": encoder_value,
            "preset": preset_value,
            "hotkey": hotkey_value,
            "audio_device": audio_device_value,
            "replay_buffer": replay_buffer_value,
            "monitor": self.monitor_combo.currentText(),

        }

        # Call the save_settings function from settings.py to save the settings to a JSON file
        settings.save_settings(settings_dict)

        # Optionally, show a status message to the user
        self.update_status("Settings saved!")



    def add_resolution_field(self, settings_layout):
        # This method is to add the resolution field to the layout.
        res_layout = QVBoxLayout()
        res_label = QLabel("Resolution (WxH):")
        self.resolution_edit = QLineEdit()  # Just create the field
        self.resolution_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        res_layout.addWidget(res_label)
        res_layout.addWidget(self.resolution_edit)
        settings_layout.addLayout(res_layout)

    def add_fps_field(self, layout):
        fps_layout = QVBoxLayout()
        fps_label = QLabel("FPS:")
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 240)
        self.fps_spin.setValue(30)  # Default value
        self.fps_spin.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        fps_layout.addWidget(fps_label)
        fps_layout.addWidget(self.fps_spin)
        layout.addLayout(fps_layout)

    def add_encoder_field(self, layout):
        enc_layout = QVBoxLayout()
        enc_label = QLabel("Encoder:")
        self.encoder = QComboBox()
        self.encoder.addItems(self.encoder_presets.keys())
        self.encoder.setCurrentText(self.settings["encoder"])
        
        # Connect the encoder selection change to update presets
        self.encoder.currentTextChanged.connect(self.update_presets)
        
        enc_layout.addWidget(enc_label)
        enc_layout.addWidget(self.encoder)
        layout.addLayout(enc_layout)

    def add_preset_field(self, layout):
        preset_layout = QVBoxLayout()
        preset_label = QLabel("Preset:")
        self.preset = QComboBox()
        self.update_presets()
        self.preset.setCurrentText(self.settings["preset"])
        preset_layout.addWidget(preset_label)
        preset_layout.addWidget(self.preset)
        layout.addLayout(preset_layout)

    def update_presets(self):
        current_encoder = self.encoder.currentText()
        self.preset.clear()  # Clear the current items
        # Add the presets based on the selected encoder
        self.preset.addItems(self.encoder_presets.get(current_encoder, []))

    def add_hotkey_field(self, layout):
        hotkey_layout = QVBoxLayout()
        hotkey_label = QLabel("Hotkey:")
        self.hotkey_edit = QLineEdit()  # Just create the field
        hotkey_layout.addWidget(hotkey_label)
        hotkey_layout.addWidget(self.hotkey_edit)
        layout.addLayout(hotkey_layout)



   
    # Adding buttons for actions    
    def add_buttons(self, main_layout):
        buttons_layout = QHBoxLayout()
        buttons_layout.setSpacing(30)

        # Save Settings Button
        save_button = QPushButton("Save Settings")
        save_button.clicked.connect(self.save_settings)  # Connect to save_settings method
        buttons_layout.addWidget(save_button)

        # Other buttons (Start Recording, Stop Recording, etc.)
        record_button = QPushButton("Start Recording")
        record_button.clicked.connect(self.start_recording)
        buttons_layout.addWidget(record_button)

        stop_button = QPushButton("Stop Recording")
        stop_button.clicked.connect(self.stop_recording)
        buttons_layout.addWidget(stop_button)

        clip_button = QPushButton("Clip Last 30s")
        clip_button.clicked.connect(self.clip_last_30_seconds)
        buttons_layout.addWidget(clip_button)

        main_layout.addLayout(buttons_layout)


    # Audio input/output selection
    def add_audio_input_output(self, main_layout):
            audio_layout = QVBoxLayout()
            audio_label = QLabel("Select Audio Input:")
            self.audio_input_combo = QComboBox()
            self.audio_input_combo.addItems(self.audio_devices["Audio Input"])

            audio_output_label = QLabel("Select Audio Output:")
            self.audio_output_combo = QComboBox()
            self.audio_output_combo.addItems(self.audio_devices["Audio Output"])

            audio_layout.addWidget(audio_label)
            audio_layout.addWidget(self.audio_input_combo)
            audio_layout.addWidget(audio_output_label)
            audio_layout.addWidget(self.audio_output_combo)

            main_layout.addLayout(audio_layout)
    
    def add_replay_buffer_checkbox(self, main_layout):
        replay_buffer_layout = QVBoxLayout()
        replay_buffer_label = QLabel("Replay Buffer:")
        self.replay_buffer_checkbox = QCheckBox()
        self.replay_buffer_checkbox.setChecked(self.settings["replay_buffer"])
        self.replay_buffer_checkbox.stateChanged.connect(self.toggle_replay_buffer)
        replay_buffer_layout.addWidget(replay_buffer_label)
        replay_buffer_layout.addWidget(self.replay_buffer_checkbox)
        main_layout.addLayout(replay_buffer_layout)

    def toggle_replay_buffer(self, state):
        if state == Qt.Checked:
            self.settings["replay_buffer"] = True
            settings.save_settings(self.settings)
            print("Replay buffer enabled")
        else:
            self.settings["replay_buffer"] = False
            settings.save_settings(self.settings)
            print("Replay buffer disabled")
        self.update_status("Replay buffer state updated")





# Method to update the presets based on the selected encoder
    def update_presets(self):
        current_encoder = self.encoder.currentText()
        self.preset.clear()  # Clear the current items
        # Add the presets based on the selected encoder
        self.preset.addItems(self.encoder_presets.get(current_encoder, []))





    def update_status(self, message):
        self.status_label.setText(message)


            
    def start_recording(self):
        if self.capture_thread and self.capture_thread.is_alive():
            print("⚠️ Already recording!")
            return

        settings = self.settings

        self.capture_thread = CaptureThread(
            self.resolution_edit.text().strip(),
            self.fps_spin.value(),
            settings["encoder"],
            preset=self.preset.currentText(),
            mode="manual",
            audio_input=f"audio={self.audio_input_combo.currentText()}",  # SOUND
            audio_output=f"audio={self.audio_output_combo.currentText()}",  # SOUND
            monitor=self.monitor_combo.currentText()
        )
        self.capture_thread.start()
        self.update_status("🔴 Recording started!")

    def list_audio_devices():
        devices = []
        CaptureThread.list_audio_devices()


    def stop_recording(self):
        """Stops manual recording."""
        if self.capture_thread and self.capture_thread.is_alive():
            print("🛑 Stopping recording...")
            self.capture_thread.stop_recording()
            self.capture_thread.join()  # ✅ Wait for thread to fully stop
            self.capture_thread = None
            self.update_status("✅ Recording stopped!")
        else:
            print("⚠️ No active FFmpeg process found. It may have already stopped.")
            self.update_status("No recording to stop")


    def clip_last_30_seconds(self):
        # This creates a one-shot thread that captures a 30s clip.
        if self.replay_buffer_checkbox.isChecked():
            clip_thread = CaptureThread(
                self.resolution_edit.text().strip(),
                self.fps_spin.value(),
                self.encoder.currentText(),
                preset=self.preset.currentText(),
                mode="clip",
                audio_input=f"audio={self.audio_input_combo.currentText()}",  # Pass audio input
                audio_output=f"audio={self.audio_output_combo.currentText()}",  # Pass audio output
                monitor=self.monitor_combo.currentText()
            )
            clip_thread.start()
            self.update_status("Clip captured.")
        else:
            print("Replay buffer is disabled - No clip has been saved")

                

            

