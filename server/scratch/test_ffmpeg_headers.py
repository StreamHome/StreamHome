import sys
import os
import asyncio

# Add server directory to Python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from services.ffmpeg import download_and_merge

async def test_ffmpeg():
    task_id = "test-ffmpeg-task"
    # A tiny video file for testing
    video_url = "C:\\Users\\deniz\\Desktop\\.all\\Projects\\tests\\video.mp4"
    audio_url = None
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://commondatastorage.googleapis.com/"
    }
    output_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "test_output.mp4"))
    
    print(f"[Test FFmpeg] Starting stream download to: {output_path}")
    success, failure = await download_and_merge(
        task_id=task_id,
        video_url=video_url,
        audio_url=audio_url,
        headers=headers,
        output_path=output_path,
        duration_secs=15.0  # 15s duration
    )
    
    result_label = "SUCCESS" if success else f"FAILURE ({failure.display if failure else 'unknown'})"
    print(f"[Test FFmpeg] Result: {result_label}")
    if success and os.path.exists(output_path):
        size_bytes = os.path.getsize(output_path)
        print(f"[Test FFmpeg] Output file exists. Size: {size_bytes} bytes")
        try:
            os.remove(output_path)
            print("[Test FFmpeg] Cleaned up temporary test file.")
        except Exception as e:
            print(f"[Test FFmpeg] Clean up error: {e}")

if __name__ == "__main__":
    asyncio.run(test_ffmpeg())
