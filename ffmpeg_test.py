import subprocess
from ffmpeg_utils import get_ffmpeg_path  # Import the fixed function

def test_ffmpeg():
    """Test if FFmpeg runs correctly"""
    ffmpeg_path = get_ffmpeg_path()
    print(f"🔍 Testing FFmpeg at: {ffmpeg_path}")

    command = [ffmpeg_path, "-version"]  # Get FFmpeg version info

    process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    if process.returncode == 0:
        print("✅ FFmpeg is working!")
        print(process.stdout)
    else:
        print("❌ FFmpeg failed to run!")
        print(process.stderr)

if __name__ == "__main__":
    test_ffmpeg()
