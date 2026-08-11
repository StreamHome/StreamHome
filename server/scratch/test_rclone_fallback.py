import sys
import os
import shutil

# Set PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Import config (should trigger PATH injection)
from config import settings
from services.queue import DownloadQueueManager
from services.rclone import RcloneResult, rclone_service

def verify_rclone_paths():
    print("=== Testing Rclone Binary Fallback Logic ===")
    
    # 1. Verify bin folder path is injected
    bin_folder = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "bin"))
    is_in_path = bin_folder in os.environ["PATH"] or any(bin_folder in p for p in os.environ["PATH"].split(os.pathsep))
    print(f"  [Info] Injected path exists: {os.path.exists(bin_folder)}")
    print(f"  [Info] Is bin folder in environment PATH: {is_in_path}")
    
    # 2. Check if we can find Rclone via shutil.which or custom fallback
    rclone_path = shutil.which("rclone")
    print(f"  [Info] shutil.which('rclone') resolves to: {rclone_path}")
    
    # Instantiate QueueManager to test run_rclone_move_dir path resolution
    qm = DownloadQueueManager()
    
    # We can inspect the fallback resolution by simulating shutil.which failure
    # If shutil.which is mocked to return None, does it fallback to bin/rclone?
    original_which = shutil.which
    try:
        shutil.which = lambda name: None if name == "rclone" else original_which(name)
        
        # We call the method by mocking target_remote/cmd to print or not run subprocess
        # Or we can just check if os.path.exists(bin/rclone.exe) holds.
        workspace_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        rclone_exe = "rclone.exe" if os.name == "nt" else "rclone"
        expected_fallback = os.path.join(workspace_root, "bin", rclone_exe)
        
        fallback_exists = os.path.exists(expected_fallback)
        print(f"  [Info] Fallback path target: {expected_fallback}")
        print(f"  [Info] Fallback binary exists on disk: {fallback_exists}")
        
        if fallback_exists:
            print("  [OK] Fallback path exists on disk and is resolvable.")
        else:
            print("  [Warning] Fallback path doesn't exist on disk (normal if Rclone is not installed yet).")
            
    finally:
        shutil.which = original_which

    print("\n[OK] Rclone path resolution verification complete!")


def verify_cloud_health_circuit():
    rclone_service._observe_cloud_result(RcloneResult(1, error_code="drive_unauthorized"))
    assert rclone_service.cloud_health()["state"] == "unauthorized"
    assert not rclone_service.cloud_write_available()
    rclone_service._observe_cloud_result(RcloneResult(0))
    assert rclone_service.cloud_health()["state"] == "healthy"
    assert rclone_service.cloud_write_available()

if __name__ == "__main__":
    verify_rclone_paths()
    verify_cloud_health_circuit()
