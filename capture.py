import ffmpeg
import os
import time
import threading
import subprocess
import re
from ffmpeg_utils import get_ffmpeg_path
import platform



class CaptureThread(threading.Thread):
    def __init__(self, resolution="1920x1080", fps=60, encoder="libx264", preset="ultrafast", mode="clip",audio_input=None,audio_output=None):
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
        self.ffmpeg_path = get_ffmpeg_path()
        self.ffmpeg_process = None  # For manual recording mode


        self.audio_input = audio_input if audio_input else self.get_default_audio_device()
        self.audio_output = audio_output if audio_output else "audio=virtual-audio-capturer" #SOUND
        self.stop_event = threading.Event()  
        


        
                

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

            # Check if the FFmpeg process is still running
            if self.ffmpeg_process.poll() is None:  # Process is still running
                try:
                    if self.ffmpeg_process.stdin:
                        self.ffmpeg_process.stdin.write("q\n")  # Send 'q' to stop recording
                        self.ffmpeg_process.stdin.flush()  # Ensure it's processed immediately
                        self.ffmpeg_process.stdin.close()  # Close stdin to indicate done
                        print("✅ 'q' command sent to FFmpeg.")
                    
                    # Wait for FFmpeg to finish
                    self.ffmpeg_process.wait(timeout=2)  # Increased timeout for safety
                    print("✅ FFmpeg process stopped cleanly.")

                except (subprocess.TimeoutExpired, BrokenPipeError, OSError, ValueError, IOError) as e:
                    # Handle errors if FFmpeg does not respond as expected
                    print(f"⚠️ FFmpeg process may already be closed or unresponsive: {e}")
                    # self.ffmpeg_process.kill()  # Force kill if necessary

                finally:
                    print("✅ Recording stopped successfully.")
                    self.ffmpeg_process = None

                    # Ensure the file was created and is not corrupt
                    if os.path.exists(self.output_file) and os.path.getsize(self.output_file) > 1000:
                        print(f"🎥 Clip saved: {self.output_file}")
                    else:
                        print("❌ Clip was not saved properly. Check FFmpeg logs.")
            else:
                print("⚠️ FFmpeg process already stopped.")
                self.ffmpeg_process = None  # Set to None if it's already stopped
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


        if self.mode == "clip":
            # Fixed 30-second clip capture
            timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
            output_file = os.path.join(self.output_dir, f"clip_{timestamp}.mp4")
            print(f"📸 Capturing last 30 seconds... Saving to {output_file}")

            # Construct the FFmpeg command manually
            ffmpeg_command = [
                self.ffmpeg_path,  # Ensure FFmpeg path is used
                "-y",  # Overwrite existing files if needed
                "-f", "gdigrab",  # Capture desktop screen
                "-framerate", str(self.fps),  # Set FPS
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


        # if self.mode == "clip":
        #     # Fixed 30-second clip capture
        #     timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        #     output_file = os.path.join(self.output_dir, f"clip_{timestamp}.mp4")
        #     print(f"📸 Capturing last 30 seconds... Saving to {output_file}")

        #     try:
        #         (
        #             ffmpeg
        #             .input("desktop", format="gdigrab", framerate=self.fps)
        #             .output(
        #                 output_file,
        #                 vcodec=self.encoder,
        #                 preset=self.preset,
        #                 crf=18,
        #                 t=30,  # 30-second clip
        #                 ss=3,  # Optionally skip the first 3 seconds
        #                 s=f"{width}x{height}",
        #             )
        #             .global_args("-nostdin")
        #             .run(overwrite_output=True)
        #         )
        #         print(f"✅ Clip saved: {output_file}")
        #     except Exception as e:
        #         print(f"❌ Error capturing clip: {e}")

        elif self.mode == "manual":
            # Continuous recording until stopped
            timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
            self.output_file = os.path.join(self.output_dir, f"record_{timestamp}.mp4")
            print(f"📸 Starting manual recording... Saving to {self.output_file}")

            command = [
                self.ffmpeg_path,
                "-y",  # Overwrite existing files if needed

                # Capture screen
                "-f", "gdigrab",
                "-framerate", str(self.fps),
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
                "-tune", "zerolatency",
                "-probesize", "32",
                "-analyzeduration", "0",
                "-fflags", "nobuffer",
                "-rtbufsize", "0M",
                "-movflags", "+faststart",  # ✅ Helps with instant playback in browsers
                self.output_file
            ]

            # **🔍 Debugging: Print the actual command Python is running**
            print(f"🔍 Running FFmpeg Command: {' '.join(command)}")

            try:
                self.ffmpeg_process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True  # ✅ Ensures proper string decoding
                )

                # ✅ Loop to check if stop_event is set
                while not self.stop_event.is_set():
                    time.sleep(0.1)  # Prevent CPU overuse

                self.stop_recording()  # Stop when event is set

            except Exception as e:
                print(f"❌ Error starting FFmpeg process: {e}")












