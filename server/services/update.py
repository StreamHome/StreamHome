import os
import sys
import subprocess
import asyncio
import time
from services.logger import logger
from services.backup import is_database_idle
import services.state as state
from config import settings

def get_git_path() -> str:
    """Find local system git binary."""
    # First check system PATH
    import shutil
    git_path = shutil.which("git")
    if git_path:
        return git_path
    # Windows fallback common paths
    common_paths = [
        r"C:\Program Files\Git\bin\git.exe",
        r"C:\Program Files (x86)\Git\bin\git.exe"
    ]
    for p in common_paths:
        if os.path.exists(p):
            return p
    return "git"

async def run_git_cmd(args: list) -> tuple:
    """Executes a git command asynchronously and returns (exit_code, stdout, stderr)."""
    git_bin = get_git_path()
    # Resolve absolute path to workspace root
    workspace_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    
    try:
        process = await asyncio.create_subprocess_exec(
            git_bin, *args,
            cwd=workspace_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        return process.returncode, stdout.decode(errors="ignore").strip(), stderr.decode(errors="ignore").strip()
    except Exception as e:
        logger.error(f"[Update Service] Failed to execute git command {args}: {e}")
        return -1, "", str(e)

async def initialize_remote() -> bool:
    """Ensures git remote is configured and points to https://github.com/WaqSea/StreamHome."""
    target_url = "https://github.com/WaqSea/StreamHome"
    
    # Check current remotes
    ret, stdout, stderr = await run_git_cmd(["remote"])
    if ret != 0:
        logger.error(f"[Update Service] Failed to list git remotes: {stderr}")
        return False
        
    remotes = stdout.splitlines()
    if "origin" not in remotes:
        logger.info(f"[Update Service] Adding remote origin pointing to: {target_url}")
        ret, stdout, stderr = await run_git_cmd(["remote", "add", "origin", target_url])
        if ret != 0:
            logger.error(f"[Update Service] Failed to add origin: {stderr}")
            return False
    else:
        logger.info(f"[Update Service] Setting remote origin URL to: {target_url}")
        ret, stdout, stderr = await run_git_cmd(["remote", "set-url", "origin", target_url])
        if ret != 0:
            logger.error(f"[Update Service] Failed to set origin URL: {stderr}")
            return False
            
    return True

async def is_git_clean() -> bool:
    """Verifies if the workspace has no uncommitted local code changes."""
    ret, stdout, stderr = await run_git_cmd(["status", "--porcelain"])
    if ret != 0:
        logger.error(f"[Update Service] Failed to run git status: {stderr}")
        return False
    # If stdout has content, the working tree is dirty
    return len(stdout.strip()) == 0

async def get_active_branch() -> str:
    """Resolves active branch name dynamically."""
    ret, stdout, stderr = await run_git_cmd(["branch", "--show-current"])
    if ret == 0 and stdout.strip():
        return stdout.strip()
    
    # Fallback for older git versions
    ret, stdout, stderr = await run_git_cmd(["symbolic-ref", "--short", "HEAD"])
    if ret == 0 and stdout.strip():
        return stdout.strip()
        
    return "master"

async def check_for_github_updates() -> bool:
    """Fetches updates and returns True if local branch is behind remote branch on origin."""
    # Ensure remote URL is set
    if not await initialize_remote():
        return False
        
    logger.info("[Update Service] Fetching latest remote branches from GitHub...")
    ret, stdout, stderr = await run_git_cmd(["fetch", "origin"])
    if ret != 0:
        logger.error(f"[Update Service] Git fetch origin failed: {stderr}")
        return False
        
    active_branch = settings.UPDATE_BRANCH
    
    # Get local and remote commit hashes
    ret_local, local_hash, _ = await run_git_cmd(["rev-parse", "HEAD"])
    ret_remote, remote_hash, _ = await run_git_cmd(["rev-parse", f"origin/{active_branch}"])
    
    if ret_local != 0 or ret_remote != 0:
        logger.error(f"[Update Service] Failed to resolve commit hashes for branch {active_branch}.")
        return False

    if settings.UPDATE_REQUIRE_SIGNED_COMMITS:
        verified, _, signature_error = await run_git_cmd(["verify-commit", remote_hash])
        if verified != 0:
            logger.error(f"[Update Service] Refusing unsigned or unverifiable update commit: {signature_error}")
            return False
        
    if local_hash != remote_hash:
        # Check if local is behind remote (i.e. is ancestor)
        ret, stdout, stderr = await run_git_cmd(["merge-base", "--is-ancestor", local_hash, remote_hash])
        if ret == 0:
            logger.info(f"[Update Service] Update available! Local HEAD ({local_hash[:8]}) is behind remote origin/{active_branch} ({remote_hash[:8]})")
            return True
        else:
            logger.warning(f"[Update Service] Local HEAD ({local_hash[:8]}) is different from remote ({remote_hash[:8]}), but not behind. Skipping auto-update to prevent conflict.")
            return False
            
    logger.info("[Update Service] StreamHome is already up-to-date.")
    return False

async def is_system_idle() -> bool:
    """
    Checks connection tracking, active downloads, and database playback sessions
    to confirm if the system is completely idle.
    """
    # 1. Check active HTTP requests
    if state.ACTIVE_HTTP_REQUESTS > 0:
        logger.info(f"[Update Service] System busy: {state.ACTIVE_HTTP_REQUESTS} active HTTP requests.")
        return False
        
    # 2. Check time since last HTTP request activity (5 minutes = 300 seconds)
    elapsed = time.time() - state.LAST_HTTP_ACTIVITY_TIMESTAMP
    if elapsed < 300:
        logger.info(f"[Update Service] System busy: Inactive for only {int(elapsed)} seconds (requires 300).")
        return False
        
    # 3. Check database queue manager active downloads & active streaming playback sessions
    if not await is_database_idle():
        # backup's is_database_idle already logs specific reasons
        return False
        
    return True

async def pull_and_install_updates() -> bool:
    """
    Safely pulls remote update. Re-installs dependencies if requirements.txt
    or package.json changed.
    """
    # 1. Be Careful: Validate working directory has no uncommitted changes
    if not await is_git_clean():
        logger.error("[Update Service] Aborting update: Local uncommitted changes detected in working directory.")
        return False
        
    active_branch = settings.UPDATE_BRANCH
    logger.info(f"[Update Service] Pulling remote updates for branch: {active_branch}...")

    ret_old, old_hash, old_error = await run_git_cmd(["rev-parse", "HEAD"])
    if ret_old != 0:
        logger.error(f"[Update Service] Cannot identify the current release: {old_error}")
        return False
    ret_fetch, _, fetch_error = await run_git_cmd(["fetch", "origin", active_branch])
    if ret_fetch != 0:
        logger.error(f"[Update Service] Git fetch failed: {fetch_error}")
        return False
    ret_remote, remote_hash, remote_error = await run_git_cmd(["rev-parse", f"origin/{active_branch}"])
    if ret_remote != 0:
        logger.error(f"[Update Service] Cannot identify the target release: {remote_error}")
        return False
    if settings.UPDATE_REQUIRE_SIGNED_COMMITS:
        verified, _, signature_error = await run_git_cmd(["verify-commit", remote_hash])
        if verified != 0:
            logger.error(f"[Update Service] Refusing unsigned or unverifiable update commit: {signature_error}")
            return False
    ret, stdout, stderr = await run_git_cmd(["merge", "--ff-only", f"origin/{active_branch}"])
    if ret != 0:
        logger.error(f"[Update Service] Fast-forward update failed: {stderr}")
        return False

    logger.info(f"[Update Service] Verified fast-forward update complete: {stdout}")
    _, changed_files, _ = await run_git_cmd(["diff", "--name-only", old_hash, remote_hash])
    
    # 2. Install dependencies if config files changed
    workspace_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    
    # Check requirements.txt
    if "server/requirements.txt" in changed_files:
        logger.info("[Update Service] requirements.txt modified. Installing python requirements...")
        pip_bin = sys.executable
        try:
            process = await asyncio.create_subprocess_exec(
                pip_bin, "-m", "pip", "install", "-r", "requirements.txt",
                cwd=workspace_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await process.communicate()
            logger.info("[Update Service] Python dependencies updated.")
        except Exception as e:
            logger.error(f"[Update Service] Failed to install requirements: {e}")
            
    # Check web package.json
    if "web/package.json" in changed_files or "web/package-lock.json" in changed_files:
        logger.info("[Update Service] web/package.json modified. Running npm install & rebuild...")
        web_dir = os.path.join(workspace_root, "web")
        import shutil
        npm_bin = shutil.which("npm")
        if npm_bin:
            try:
                # Run npm install
                process_install = await asyncio.create_subprocess_exec(
                    npm_bin, "ci",
                    cwd=web_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await process_install.communicate()
                
                # Run npm run build
                process_build = await asyncio.create_subprocess_exec(
                    npm_bin, "run", "build",
                    cwd=web_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await process_build.communicate()
                logger.info("[Update Service] Web dependencies and build asset bundle updated.")
            except Exception as e:
                logger.error(f"[Update Service] Failed to rebuild web frontend: {e}")
        else:
            logger.error("[Update Service] npm not found in path. Cannot rebuild frontend.")
            
    return True

def self_restart_server():
    """Spawns a fresh Python process of main.py and exits the parent process cleanly."""
    logger.info("[Update Service] Initiating server process self-restart...")
    args = sys.argv
    executable = sys.executable
    server_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    try:
        if sys.platform == 'win32':
            # Detached process group on Windows
            subprocess.Popen(
                [executable] + args,
                cwd=server_dir,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                close_fds=True
            )
        else:
            # POSIX process group daemon reload
            subprocess.Popen(
                [executable] + args,
                cwd=server_dir,
                preexec_fn=os.setpgrp,
                close_fds=True
            )
        logger.info("[Update Service] New process spawned successfully. Exiting parent process...")
        os._exit(0)
    except Exception as e:
        logger.error(f"[Update Service] Failed to auto-restart server: {e}")
