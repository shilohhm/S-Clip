import os
import shutil
import subprocess

def get_ffmpeg_path():
    """Returns the path to the FFmpeg binary, checking multiple possible locations."""
    ffmpeg_names = ["ffmpeg.exe", "ffmpeg"]  # Windows & Linux/Mac
    possible_dirs = [
        os.path.join(os.path.dirname(__file__), 'ffmpeg-7.1-essentials_build', 'bin'),  # S-Clip folder
        os.path.join(os.getcwd(), 'ffmpeg-7.1-essentials_build', 'bin'),  # Current working directory
        os.getenv("PATH"),  # System PATH
    ]

    for ffmpeg_dir in possible_dirs:
        for name in ffmpeg_names:
            ffmpeg_path = os.path.join(ffmpeg_dir, name)
            if os.path.exists(ffmpeg_path):
                return ffmpeg_path  # ✅ Found FFmpeg!

    # Last resort: Check if FFmpeg is installed globally
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        return ffmpeg_path

    
    raise FileNotFoundError("⚠️ FFmpeg not found! Please ensure it's installed or placed in the expected directory.")

def run_ffmpeg(command):
    """Runs FFmpeg with the given command."""
    ffmpeg_path = get_ffmpeg_path()
    return subprocess.run([os.path.normpath(ffmpeg_path), *command], capture_output=True, text=True)
    