import ffmpeg
import os
import time
import threading
import subprocess
import re
from ffmpeg_utils import get_ffmpeg_path
import platform
from screeninfo import get_monitors




class CaptureThread(threading.Thread):
    def __init__(self, resolution, fps, encoder, preset, mode,audio_input,audio_output,monitor):
        """
        mode: "clip" for a one-shot 30-second clip,
              "manual" for continuous recording (stopped manually).
        """
        super().__init__()
        self.output_dir = os.path.join(os.path.dirname(__file__), "clips")
        os.makedirs(self.output_dir, exist_ok=True)

        self.resolution = resolution
        self.fps = fps
        self.encoder = encoder
        self.preset = preset
        self.mode = mode
        self.monitor = monitor
        self.ffmpeg_path = get_ffmpeg_path()
        self.ffmpeg_process = None  # For manual recording mode
        


        self.audio_input = audio_input if audio_input else self.get_default_audio_device()
        self.audio_output = audio_output if audio_output else "audio=virtual-audio-capturer" #SOUND
        self.stop_event = threading.Event()
        
        


    

    @staticmethod
    def list_monitors():
        """Detect available monitors and their coordinates."""
        monitors = get_monitors()
        monitor_list = {}

        for i, m in enumerate(monitors):
            monitor_list[f"Monitor {i+1}"] = {
                "x": m.x,
                "y": m.y,
                "width": m.width,
                "height": m.height,
            }

        return monitor_list
            
                

    @staticmethod
    def list_audio_devices():
        """Lists all available audio input and output devices using FFmpeg."""
        ffmpeg_path = get_ffmpeg_path()  # Ensure FFmpeg path is correct

        try:
            result = subprocess.run(
                [ffmpeg_path, "-list_devices", "true", "-f", "dshow", "-i", "dummy"], 
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )

            devices = []
            for line in result.stderr.split("\n"):  # FFmpeg outputs device list to stderr
                match = re.search(r'"(.*?)" \(audio\)', line)  # Extract device names
                if match:
                    devices.append(match.group(1))

            return devices

        except Exception as e:
            print(f"❌ Error detecting audio devices: {e}")
            return []
        

    @staticmethod
    def get_default_audio_device():
        """Finds the user's default audio input device for FFmpeg."""
        audio_devices = CaptureThread.list_audio_devices()

        if not audio_devices:
            print("No audio devices found")
            return None
        else:
            return audio_devices[0]
    

    def stop_recording(self):
        """Gracefully stops recording by sending 'q' to FFmpeg."""
        if self.ffmpeg_process:
            print("⏹ Stopping recording...")
            self.stop_event.set()

            if self.ffmpeg_process.poll() is None:  # Process is still running
                try:
                    if self.ffmpeg_process.stdin:
                        self.ffmpeg_process.stdin.write("q\n")
                        self.ffmpeg_process.stdin.flush()
                        self.ffmpeg_process.stdin.close()
                        print("✅ 'q' command sent to FFmpeg.")

                    self.ffmpeg_process.wait(timeout=2)  # Give FFmpeg time to stop

                except (subprocess.TimeoutExpired, BrokenPipeError, OSError, ValueError, IOError) as e:
                    print(f"⚠️ FFmpeg process may already be closed: {e}")

                finally:
                    print("✅ Recording stopped successfully.")
                    self.ffmpeg_process = None

                    # Verify clip exists before accessing
                    if self.output_file and os.path.exists(self.output_file) and os.path.getsize(self.output_file) > 1000:
                        print(f"🎥 Clip saved: {self.output_file}")
                    else:
                        print("❌ Clip was not saved properly.")
        else:
            print("⚠️ No active FFmpeg process found.")



    def run(self):
        width, height = map(int, self.resolution.split("x"))
        
        try:
            print(f"FFmpeg path is: {self.ffmpeg_path}")


            # Detect audio devices again before capturing
            detected_audio = CaptureThread.list_audio_devices()
            if not detected_audio:
                print("⚠️ No audio devices detected, recording without audio.")
                audio_input_cmd = []
            else:
                audio_device = detected_audio[0]  # Default to first detected device
                print(f"🎤 Using audio device: {audio_device}")
                audio_input_cmd = ["-f", "dshow", "-i", f"audio={audio_device}"]

        except Exception as e:
            print(f"❌ Error detecting audio devices: {e}")


        selected_monitor = self.monitor  # Monitor name (e.g., "Monitor 1")
        monitor_info = CaptureThread.list_monitors().get(selected_monitor, None)

        if not monitor_info:
            print(f"⚠️ Selected monitor {selected_monitor} not found! Defaulting to full screen.")
            monitor_x, monitor_y, width, height = 0, 0, self.resolution.split("x")
        else:
            monitor_x, monitor_y = monitor_info["x"], monitor_info["y"]
            width, height = monitor_info["width"], monitor_info["height"]

        if self.mode == "clip":
            timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
            output_file = os.path.join(self.output_dir, f"clip_{timestamp}.mp4")
            # Fixed 30-second clip capture
            print(f"📸 Capturing last 30 seconds... Saving to {output_file}")

            # Construct the FFmpeg command manually
            ffmpeg_command = [
                self.ffmpeg_path,  # Ensure FFmpeg path is used
                "-y",  # Overwrite existing files if needed
                "-f", "gdigrab",  # Capture desktop screen
                "-framerate", str(self.fps),  # Set FPS
                "-offset_x", str(monitor_x),  # Start capture at the monitor's x position
                "-offset_y", str(monitor_y),  # Start capture at the monitor's y position
                "-video_size", f"{width}x{height}",  # Capture only this monitor
                "-i", "desktop",  # Input source (screen capture)
                "-f", "dshow",  # Audio input format
                "-i", f"audio={audio_device}",  # Audio device from GUI
                "-c:v", self.encoder,  # Video encoder
                "-preset", self.preset,  # Encoder preset
                "-crf", "18",  # Constant Rate Factor (quality)
                "-pix_fmt", "yuv420p",  # Pixel format (standard)
                "-s", f"{width}x{height}",  # Resolution
                "-c:a", "libmp3lame",  # Audio codec (MP3)
                "-b:a", "192k",  # Audio bitrate
                "-t", "30",  # Set the duration of the clip (30 seconds)
                "-ss", "3",  # Optionally skip the first 3 seconds
                "-movflags", "+faststart",  # Improve playback start time
                output_file
            ]

            # Debugging the output file path and command
            print(f"Output File Path: {output_file}")
            print(f"🔍 Running FFmpeg Command: {' '.join(ffmpeg_command)}")  # Debug FFmpeg command

            try:
                # Run the FFmpeg command
                subprocess.run(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

                print(f"✅ Clip saved: {output_file}")
            except Exception as e:
                print(f"❌ Error capturing clip: {e}")


        
        elif self.mode == "manual":
            timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
            self.output_file = os.path.join(self.output_dir, f"recording_{timestamp}.mp4")
            # Continuous recording until stopped
            print(f"📸 Starting manual recording... Saving to {self.output_file}")

            command = [
                self.ffmpeg_path,
                "-y",  # Overwrite existing files if needed

                # Capture screen
                "-f", "gdigrab",
                "-framerate", str(self.fps),
                "-offset_x", str(monitor_x),  # Start capture at the monitor's x position
                "-offset_y", str(monitor_y),  # Start capture at the monitor's y position
                "-video_size", f"{width}x{height}",  # Capture only this monitor
                "-i", "desktop",

                # Audio Input (Microphone)
                "-f", "dshow",
                "-i", self.audio_input,

                # Set output video & audio
                "-map", "0:v",  # Map video from gdigrab
                "-map", "1:a",  # Map audio from microphone
                "-c:v", self.encoder,
                "-preset", self.preset,
                "-crf", "18",
                "-pix_fmt", "yuv420p",  # ✅ Fix for playback issues
                "-s", f"{width}x{height}",
                "-c:a", "libmp3lame",
                "-b:a", "192k",
                "-probesize", "32",
                "-analyzeduration", "0",
                "-fflags", "nobuffer",
                "-rtbufsize", "0M",
                "-movflags", "+faststart",  # ✅ Helps with instant playback in browsers
                self.output_file
                
            ]
            
            if self.encoder == "libx264":
                command.extend(["-tune", "zerolatency"])


           

            

            try:
                self.ffmpeg_process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True  # ✅ Ensures proper string decoding
                )
                
                print("🔍 FFmpeg Process Started... Waiting for output...")
                for line in self.ffmpeg_process.stderr:
                    print(f"FFmpeg: {line.strip()}")

                # ✅ Loop to check if stop_event is set
                while not self.stop_event.is_set():
                    time.sleep(0.1)  # Prevent CPU overuse

                self.stop_recording()  # Stop when event is set

            except Exception as e:
                print(f"❌ Error starting FFmpeg process: {e}")












