
import os
import subprocess

def get_ffmpeg_path():
    """Returns the path to the static FFmpeg binary."""
    ffmpeg_dir = os.path.join(os.path.dirname(__file__), 'ffmpeg-7.1-essentials_build', 'bin')
    os.chdir(ffmpeg_dir)
    ffmpeg_path = 'ffmpeg.exe'
    if not os.path.exists(ffmpeg_path):
        print(f"⚠️ FFmpeg not found at {ffmpeg_path}! Make sure it's correctly extracted.")
    
    return ffmpeg_path


def run_ffmpeg(command):
    """Runs FFmpeg with the given command."""
    ffmpeg_path = get_ffmpeg_path()
    return subprocess.run([os.path.normpath(ffmpeg_path), *command], capture_output=True, text=True)