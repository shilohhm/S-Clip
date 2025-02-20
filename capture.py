import ffmpeg
import os
import time
import threading
import subprocess
from ffmpeg_utils import get_ffmpeg_path

class CaptureThread(threading.Thread):
    def __init__(self, resolution="1920x1080", fps=60, encoder="libx264", preset="ultrafast", mode="clip"):
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

    def run(self):
        width, height = map(int, self.resolution.split("x"))
        if self.mode == "clip":
            # Fixed 30-second clip capture
            timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
            output_file = os.path.join(self.output_dir, f"clip_{timestamp}.mp4")
            print(f"📸 Capturing last 30 seconds... Saving to {output_file}")
            try:
                (
                    ffmpeg
                    .input("desktop", format="gdigrab", framerate=self.fps)
                    .output(
                        output_file,
                        vcodec=self.encoder,
                        preset=self.preset,
                        crf=18,
                        t=30,     # 30-second clip
                        ss=3,     # optionally skip the first 3 seconds
                        s=f"{width}x{height}"
                    )
                    .global_args("-nostdin")
                    .run(overwrite_output=True)
                )
                print(f"✅ Clip saved: {output_file}")
            except Exception as e:
                print(f"❌ Error capturing clip: {e}")
        elif self.mode == "manual":
            # Continuous recording until stopped gracefully
            timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
            self.output_file = os.path.join(self.output_dir, f"record_{timestamp}.mp4")
            print(f"📸 Starting manual recording... Saving to {self.output_file}")
            command = [
                self.ffmpeg_path,
                "-f", "gdigrab",
                "-framerate", str(self.fps),
                "-i", "desktop",
                "-vcodec", self.encoder,
                "-preset", self.preset,
                "-crf", "18",
                "-s", f"{width}x{height}",
                self.output_file
            ]
            # Attach stdin so we can send a quit command
            self.ffmpeg_process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            # Wait for the process to finish (this will block until it terminates after receiving 'q')
            self.ffmpeg_process.wait()
            print(f"✅ Manual recording saved: {self.output_file}")

    def stop_recording(self):
        if self.mode == "manual" and self.ffmpeg_process:
            print("⏹ Stopping manual recording gracefully...")
            try:
                # Send 'q' to FFmpeg's stdin to quit gracefully.
                self.ffmpeg_process.communicate(input=b"q\n", timeout=5)
            except subprocess.TimeoutExpired:
                self.ffmpeg_process.kill()
                self.ffmpeg_process.wait()
            self.ffmpeg_process = None
        # For clip mode, nothing needs to be stopped since it terminates automatically.
