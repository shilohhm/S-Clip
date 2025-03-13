from capture import CaptureThread
#test audio devices
devices = CaptureThread.list_audio_devices()
print("Detected Audio Devices:", devices)


