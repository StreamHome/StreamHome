import os
import sys

# Force standard output and standard error to UTF-8 encoding on Windows to prevent UnicodeEncodeError
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

import secrets
import asyncio
import bcrypt
import shutil
import pyotp
import httpx
import re
from typing import Optional
from sqlmodel import SQLModel, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.align import Align

# Inject local bin/ path into system PATH for discovery of ffmpeg/ffprobe/rclone
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
bin_path = os.path.join(project_root, "bin")
if os.path.exists(bin_path) and bin_path not in os.environ.get("PATH", ""):
    os.environ["PATH"] = bin_path + os.pathsep + os.environ.get("PATH", "")

from config import settings, config_dir
from models import User, DownloadTask, Movie, Episode, IntegrationCredential
from db import init_db
from services.secret_crypto import protect_secret
from services.integration_auth import integration_token_hash

console = Console()

def get_default_rclone_remote() -> str:
    rclone_path = shutil.which("rclone")
    if not rclone_path:
        fallback_exe = "rclone.exe" if sys.platform == "win32" else "rclone"
        fallback_path = os.path.join(bin_path, fallback_exe)
        if os.path.exists(fallback_path):
            rclone_path = fallback_path
            
    if rclone_path:
        import subprocess
        try:
            proc = subprocess.run([rclone_path, "listremotes"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=3)
            if proc.returncode == 0:
                remotes = [r.strip() for r in proc.stdout.splitlines() if r.strip()]
                if remotes:
                    first = remotes[0]
                    if not first.endswith(":"):
                        first += ":"
                    return f"{first}media"
        except Exception:
            pass
    return "gdrive:media"

# ─────────────────────────── Input Utilities ───────────────────────────

if sys.platform == "win32":
    import msvcrt
    
    def getch() -> str:
        return msvcrt.getwch()
        
    def kbhit() -> bool:
        return msvcrt.kbhit()
else:
    import termios
    import tty
    import select as sys_select
    
    def getch() -> str:
        import os
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch_bytes = os.read(fd, 1)
            if not ch_bytes:
                return ""
            ch = ch_bytes.decode("utf-8", errors="ignore")
            if ch == "\x1b":
                dr, _, _ = sys_select.select([fd], [], [], 0.05)
                if dr:
                    ch2_bytes = os.read(fd, 1)
                    if ch2_bytes:
                        ch2 = ch2_bytes.decode("utf-8", errors="ignore")
                        if ch2 in ("[", "O"):
                            dr2, _, _ = sys_select.select([fd], [], [], 0.05)
                            if dr2:
                                ch3_bytes = os.read(fd, 1)
                                if ch3_bytes:
                                    ch3 = ch3_bytes.decode("utf-8", errors="ignore")
                                    return f"\x1b{ch2}{ch3}"
                        return f"\x1b{ch2}"
                return "\x1b"
            
            # Read any buffered bytes immediately to support copy/paste
            seq = [ch]
            while True:
                r, _, _ = sys_select.select([fd], [], [], 0.005)
                if r:
                    next_bytes = os.read(fd, 1)
                    if next_bytes:
                        seq.append(next_bytes.decode("utf-8", errors="ignore"))
                    else:
                        break
                else:
                    break
            return "".join(seq)
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, old_settings)
        
    def kbhit() -> bool:
        fd = sys.stdin.fileno()
        dr, dw, de = sys_select.select([fd], [], [], 0)
        return len(dr) > 0

def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")

def get_key():
    """Read a single keypress with arrow key support (Cross-platform)."""
    if sys.platform == "win32":
        ch = getch()
        if ch in ("\xe0", "\x00"):
            ch2 = getch()
            return {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT"}.get(ch2)
        if ch == "\r":
            return "ENTER"
        if ch == "\x1b":
            return "ESC"
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch
    else:
        ch = getch()
        if ch in ("\x1b[A", "\x1bOA"): return "UP"
        elif ch in ("\x1b[B", "\x1bOB"): return "DOWN"
        elif ch in ("\x1b[D", "\x1bOD"): return "LEFT"
        elif ch in ("\x1b[C", "\x1bOC"): return "RIGHT"
        elif ch == "\x1b": return "ESC"
        if ch in ("\r", "\n"):
            return "ENTER"
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch

def get_text_input(prompt_text: str, default_val: str = "", is_masked: bool = False) -> str:
    """
    Custom text input reader that replaces built-in input().
    Captures keys one-by-one to support real-time asterisk masking
    and instant ESC cancellation without requiring Enter.
    """
    sys.stdout.write(prompt_text)
    sys.stdout.flush()
    
    chars = []
    
    while True:
        ch = getch()
        
        if ch in ("\r", "\n"):  # Enter committed
            sys.stdout.write("\n")
            sys.stdout.flush()
            result = "".join(chars).strip()
            return result if result else default_val
            
        elif ch.startswith("\x1b"):  # ESC or Arrow key
            if ch != "\x1b":
                continue  # Ignore arrow key sequence
            sys.stdout.write("\n")
            sys.stdout.flush()
            return "ESC"
            
        elif ch in ("\x08", "\x7f"):  # Backspace handling
            if chars:
                chars.pop()
                sys.stdout.write("\b \b")
                sys.stdout.flush()
                
        elif ch == "\x03":  # Ctrl+C abort
            raise KeyboardInterrupt
            
        elif sys.platform == "win32" and ch in ("\xe0", "\x00"):  # Skip prefix byte for arrows/special function keys on Windows
            getch()
            
        else:  # Append standard characters
            for char in ch:
                try:
                    if ord(char) >= 32:
                        chars.append(char)
                        if is_masked:
                            sys.stdout.write("*")
                        else:
                            sys.stdout.write(char)
                except Exception:
                    pass
            sys.stdout.flush()

def prompt_input(label: str, default: str = "", is_masked: bool = False) -> str:
    """Styled interface prompt with safe fallback to prevent Rich parsing syntax breaks."""
    if default and is_masked:
        console.print(f"   [bright_yellow][?][/bright_yellow] {label} [********]: ", end="")
    elif default:
        console.print(f"   [bright_yellow][?][/bright_yellow] {label} [{default}]: ", end="")
    else:
        console.print(f"   [bright_yellow][?][/bright_yellow] {label}: ", end="")
        
    return get_text_input("", default, is_masked)

def get_inline_input(is_masked: bool = False) -> str:
    """Read inline text input inside terminal bracket style."""
    val = ""
    while True:
        ch = getch()
        if ch in ("\r", "\n"):
            break
        elif ch.startswith("\x1b"): # ESC or Arrow key on Unix
            if ch != "\x1b":
                continue # Ignore arrow key sequence
            val = "ESC"
            break
        elif sys.platform == "win32" and ch in ("\xe0", "\x00"): # Arrow/Special keys header on Windows
            try:
                getch() # Consume second character
            except Exception:
                pass
            continue
        elif ch in ("\x08", "\x7f"): # Backspace
            if len(val) > 0:
                val = val[:-1]
                sys.stdout.write("\b \b")
                sys.stdout.flush()
        else:
            for char in ch:
                try:
                    if ord(char) >= 32:
                        val += char
                        if is_masked:
                            sys.stdout.write("*")
                        else:
                            sys.stdout.write(char)
                except Exception:
                    pass
            sys.stdout.flush()
    
    if val != "ESC":
        sys.stdout.write(" ]\n")
        sys.stdout.flush()
    return val

# ─────────────────────────── .env File Manager ───────────────────────────

def update_env_file(key: str, value: str):
    """Write or update a key=value pair in the .env file."""
    cli_dir = os.path.dirname(os.path.abspath(__file__))
    env_file = os.path.join(cli_dir, ".env")
    lines = []
    if os.path.exists(env_file):
        with open(env_file, "r") as f:
            lines = f.readlines()
    
    key_exists = False
    new_lines = []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            new_lines.append(f'{key}="{value}"\n')
            key_exists = True
        else:
            new_lines.append(line)
    
    if not key_exists:
        new_lines.append(f'{key}="{value}"\n')
    
    with open(env_file, "w") as f:
        f.writelines(new_lines)
    
    # Update current runtime settings as well
    setattr(settings, key, value)

# ─────────────────────────── Arrow-Key Menu ───────────────────────────

def draw_main_header():
    """Draw the main control center header."""
    
    console.print(Panel(
        "[bold white]THE PROJECT — SERVER CONTROL CENTER[/bold white]\n"
        "[dim]Strictly Secure Terminal-Only Administration Panel[/dim]",
        border_style="bright_cyan",
        width=68,
        padding=(0, 2)
    ))

def arrow_menu(options: list, icons: list, is_sub_menu: bool = False) -> int:
    """Interactive arrow-key navigable menu. Returns selected index or -1 on ESC."""
    selected = 0
    count = len(options)
    
    while True:
        clear_screen()
        draw_main_header()
        console.print()
        
        for i, opt in enumerate(options):
            icon = icons[i]
            num = f"[{i + 1}]"
            if i == selected:
                console.print(f"   [bold bright_cyan]▸[/bold bright_cyan] [bold white]{num} {icon}  {opt}[/bold white]")
            else:
                console.print(f"     [dim]{num} {icon}  {opt}[/dim]")
        
        console.print()
        console.print(f"[dim]{'─' * 70}[/dim]")
        
        if is_sub_menu:
            console.print("[dim italic]  ↑↓ Navigate  ·  Enter Select  ·  Esc Back[/dim italic]")
        else:
            console.print("[dim italic]  ↑↓ Navigate  ·  Enter Select  ·  Esc Exit[/dim italic]")
            
        key = get_key()
        if key == "UP":
            selected = (selected - 1) % count
        elif key == "DOWN":
            selected = (selected + 1) % count
        elif key == "ENTER":
            return selected
        elif key == "ESC":
            return -1 if is_sub_menu else (count - 1)
        elif isinstance(key, str) and key.isdigit():
            idx = int(key) - 1
            if 0 <= idx < count:
                return idx

# ─────────────────────── 1. Configure Settings ───────────────────────

async def configure_settings():
    """Sub-menu routing isolated configuration prompts for individual blocks."""
    sub_options = [
        "Storage Settings",
        "API Key Manager Center (Bearer Token)",
        "TMDB Catalog Metadata Secrets",
        "Back to Main Menu"
    ]
    sub_icons = ["💾", "🔑", "🎬", "↩️"]
    
    while True:
        choice = arrow_menu(sub_options, sub_icons, is_sub_menu=True)
        
        if choice == -1 or choice == 3:  # ESC pressed or Back selected
            return
            
        clear_screen()
        
        
        collected = {}
        
        # ─── CASE 1: STORAGE SETTINGS (TUI Menu style) ───
        if choice == 0:
            while True:
                clear_screen()
                
                console.print(Panel("[bold white]💾 STORAGE SETTINGS CONFIGURATION[/bold white]", border_style="bright_yellow", width=68))
                console.print()
                
                storage_options = [
                    f"Storage Engine Mode     : [cyan]{settings.STORAGE_ENGINE}[/cyan]",
                    f"Rclone Remote Path      : [cyan]{settings.RCLONE_REMOTE_PATH}[/cyan]",
                    "Run Rclone Configuration Wizard (rclone config)",
                    "Back to Config Menu"
                ]
                storage_icons = ["⚙️", "📂", "⚡", "↩️"]
                
                sel = arrow_menu(storage_options, storage_icons, is_sub_menu=True)
                if sel == -1 or sel == 3:
                    break
                    
                clear_screen()
                
                
                if sel == 0:
                    console.print(Panel("[bold white]⚙️ EDIT STORAGE ENGINE MODE[/bold white]", border_style="bright_yellow", width=68))
                    console.print()
                    val = prompt_input("Select Storage Engine Mode (LOCAL/CLOUD)", settings.STORAGE_ENGINE)
                    if val != "ESC" and val.upper() in ["LOCAL", "CLOUD"]:
                        settings.STORAGE_ENGINE = val.upper()
                        update_env_file("STORAGE_ENGINE", val.upper())
                        settings.save_to_json()
                        console.print("\n   [bold bright_green][✓][/bold bright_green] [white]Storage Engine Mode updated and saved![/white]")
                    else:
                        console.print("\n   [bold bright_red][✗][/bold bright_red] [dim]Invalid value or operation cancelled.[/dim]")
                    console.print("   [dim]Press Enter to continue...[/dim]", end="")
                    get_text_input("", default_val="")
                    
                elif sel == 1:
                    console.print(Panel("[bold white]📂 EDIT RCLONE REMOTE PATH[/bold white]", border_style="bright_yellow", width=68))
                    console.print()
                    val = prompt_input("Enter Rclone Remote Path", settings.RCLONE_REMOTE_PATH)
                    if val != "ESC" and val.strip():
                        settings.RCLONE_REMOTE_PATH = val.strip()
                        update_env_file("RCLONE_REMOTE_PATH", val.strip())
                        settings.save_to_json()
                        console.print("\n   [bold bright_green][✓][/bold bright_green] [white]Rclone Remote Path updated and saved![/white]")
                    else:
                        console.print("\n   [bold bright_red][✗][/bold bright_red] [dim]Operation cancelled.[/dim]")
                    console.print("   [dim]Press Enter to continue...[/dim]", end="")
                    get_text_input("", default_val="")

                elif sel == 2:
                    rclone_path = shutil.which("rclone")
                    if not rclone_path:
                        bin_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin"))
                        rclone_exe = "rclone.exe" if os.name == "nt" else "rclone"
                        fallback_path = os.path.join(bin_path, rclone_exe)
                        if os.path.exists(fallback_path):
                            rclone_path = fallback_path
                            
                    if rclone_path:
                        clear_screen()
                        console.print(Panel("[bold white]⚡ RUNNING INTERACTIVE RCLONE CONFIGURATION[/bold white]", border_style="bright_yellow", width=68))
                        console.print("Starting `rclone config`... Please follow the prompts to configure your cloud remote.\n")
                        import subprocess
                        try:
                            # Run it interactively directly in the current terminal process
                            subprocess.run([rclone_path, "config"], check=True)
                        except Exception as e:
                            console.print(f"\n[bold red][✗] Error running rclone config: {e}[/bold red]")
                    else:
                        console.print("\n[bold red][✗] Rclone binary not found. Please run setup first.[/bold red]")
                        
                    console.print("\n   [dim]Press Enter to continue...[/dim]", end="")
                    get_text_input("", default_val="")
            
        # ─── CASE 2: INTERACTIVE INGEST API KEY PANEL (FULLY DETACHED AS REQUESTED) ───
        elif choice == 1:
            key_options = [
                "✨ Generate Secure 32-Char API Key",
                "📋 View Current Active API Key",
                "🗑️  Revoke / Delete API Key",
                "↩️  Back to Config Menu"
            ]
            key_icons = ["⚡", "🔍", "🔥", "↩️"]
            
            while True:
                key_choice = arrow_menu(key_options, key_icons, is_sub_menu=True)
                if key_choice == -1 or key_choice == 3:
                    break
                    
                clear_screen()
                
                
                # Sub-Case 1: Generate Key
                if key_choice == 0:
                    console.print(Panel("[bold white]⚡ AUTONOMOUS API KEY GENERATION[/bold white]", border_style="bright_yellow", width=68))
                    console.print("\n[dim]   Generating a new cryptographically secure 32-character API key...[/dim]\n")
                    new_key = secrets.token_urlsafe(36)
                    
                    engine = create_async_engine(settings.DATABASE_URL)
                    async with AsyncSession(engine) as session:
                        existing = (await session.execute(select(IntegrationCredential))).scalars().all()
                        for credential in existing:
                            if "ingest" in credential.scopes and not credential.revoked_at:
                                credential.revoked_at = __import__("time").time()
                                session.add(credential)
                        credential = IntegrationCredential(
                            id=__import__("uuid").uuid4().hex,
                            name="CLI MediaSender",
                            token_hash=integration_token_hash(new_key),
                        )
                        credential.scopes = ["ingest"]
                        session.add(credential)
                        await session.commit()
                    console.print(f"   [bold bright_green][✓][/bold bright_green] [white]New scoped ingestion key generated (shown once):[/white]")
                    console.print(f"   👉 [bold bright_yellow]{new_key}[/bold bright_yellow]\n")
                    console.print("   [bold red]⚠️  IMPORTANT: COPY THIS KEY NOW. IT CANNOT BE VIEWED AGAIN.[/bold red]")
                
                # Sub-Case 2: View Key
                elif key_choice == 1:
                    console.print(Panel("[bold white]🔍 VIEW ACTIVE INGESTION API KEY[/bold white]", border_style="bright_yellow", width=68))
                    engine = create_async_engine(settings.DATABASE_URL)
                    async with AsyncSession(engine) as session:
                        credentials = (await session.execute(select(IntegrationCredential))).scalars().all()
                        active = [item for item in credentials if "ingest" in item.scopes and not item.revoked_at]
                    if active:
                        console.print(f"\n   [bold bright_green]{len(active)} active scoped ingestion credential(s).[/bold bright_green]")
                        for credential in active:
                            console.print(f"   • {credential.name} ({credential.id})")
                        console.print("   [dim]Raw keys are intentionally show-once and cannot be recovered.[/dim]\n")
                    else:
                        console.print("\n   [bold bright_red][✗][/bold bright_red] [white]No active ingestion credential exists.[/white]\n")
                        
                # Sub-Case 3: Delete Key
                elif key_choice == 2:
                    console.print(Panel("[bold white]🔥 REVOKE INGEST API KEY CONTROL[/bold white]", border_style="bright_red", width=68))
                    confirm = prompt_input("Are you absolutely sure you want to completely clear the active API key? (y/N)", "N")
                    if confirm.lower() == "y" and confirm != "ESC":
                        engine = create_async_engine(settings.DATABASE_URL)
                        async with AsyncSession(engine) as session:
                            existing = (await session.execute(select(IntegrationCredential))).scalars().all()
                            for credential in existing:
                                if "ingest" in credential.scopes and not credential.revoked_at:
                                    credential.revoked_at = __import__("time").time()
                                    session.add(credential)
                            await session.commit()
                        console.print("\n   [bold bright_green][✓][/bold bright_green] [white]API key successfully deleted. Ingestion pipeline locked.[/white]")
                    else:
                        console.print("\n   [bold bright_yellow][!][/bold bright_yellow] [dim]Revocation cancelled.[/dim]")
                
                console.print("\n   [dim]Press Enter to return to Key Manager...[/dim]", end="")
                get_text_input("", default_val="")
            
        # ─── CASE 3: TMDB SECRETS ───
        elif choice == 2:
            console.print(Panel("[bold white]🎬 TMDB API CONNECTIONS CONFIGURATION[/bold white]", border_style="bright_yellow", width=68))
            console.print("\n[dim italic]   Press ESC at any prompt to abort and return to settings menu[/dim italic]\n")
            
            val = prompt_input("Enter TMDB API Key (v3)", settings.TMDB_API_KEY, is_masked=True)
            if val == "ESC": continue
            collected["TMDB_API_KEY"] = val
            
            val = prompt_input("Enter TMDB Read Access Token (v4 Bearer)", settings.TMDB_READ_ACCESS_TOKEN, is_masked=True)
            if val == "ESC": continue
            collected["TMDB_READ_ACCESS_TOKEN"] = val
 
            console.print()
            confirm = prompt_input("Save changes for TMDB API configurations? (y/N)", "N")
            if confirm.lower() == "y" and confirm != "ESC":
                for key, value in collected.items():
                    if value: update_env_file(key, value)
                console.print("\n   [bold bright_green][✓][/bold bright_green] [white]TMDB keys saved to .env![/white]")
            else:
                console.print("\n   [bold bright_red][✗][/bold bright_red] [dim]Changes discarded.[/dim]")
            console.print("   [dim]Press Enter to continue...[/dim]", end="")
            get_text_input("", default_val="")

# ─────────────────────── 2. Register User ───────────────────────

async def register_user():
    """Account registration flow with password hashing. Supports ESC aborting."""
    clear_screen()
    
    console.print(Panel(
        "[bold white]👤   ACCOUNT REGISTRATION PORTAL[/bold white]\n"
        "[dim italic]Press ESC during input to instantly abort and return to menu[/dim italic]",
        border_style="bright_magenta",
        width=68,
        padding=(0, 2)
    ))
    console.print()
    
    email = prompt_input("Enter User Account Email Address")
    if email == "ESC": return
    if not email or "@" not in email:
        console.print()
        console.print("   [bold bright_red][✗][/bold bright_red] [white]Invalid email address![/white]")
        console.print("   [dim]Press Enter to return...[/dim]", end="")
        get_text_input("", default_val="")
        return
    
    password = prompt_input("Create Secure Account Password", is_masked=True)
    if password == "ESC": return
    if len(password) < 6:
        console.print()
        console.print(
            "   [bold bright_red][✗][/bold bright_red] "
            "[white]Password must be at least 6 characters long![/white]"
        )
        console.print("   [dim]Press Enter to return...[/dim]", end="")
        get_text_input("", default_val="")
        return
    
    confirm_pw = prompt_input("Confirm Account Password", is_masked=True)
    if confirm_pw == "ESC": return
    if password != confirm_pw:
        console.print()
        console.print("   [bold bright_red][✗][/bold bright_red] [white]Passwords do not match![/white]")
        console.print("   [dim]Press Enter to return...[/dim]", end="")
        get_text_input("", default_val="")
        return
    
    # Hash using bcrypt
    hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    
    # Save to SQLite asynchronously
    engine = create_async_engine(settings.DATABASE_URL)
    async with AsyncSession(engine) as session:
        statement = select(User).where(User.email == email)
        result = await session.exec(statement)
        existing_user = result.first()
        if existing_user:
            console.print()
            console.print(
                "   [bold bright_red][✗][/bold bright_red] "
                "[white]User with this email already exists![/white]"
            )
            console.print("   [dim]Press Enter to return...[/dim]", end="")
            get_text_input("", default_val="")
            return
        
        new_user = User(email=email, password_hash=hashed)
        session.add(new_user)
        await session.commit()
    
    console.print()
    console.print(
        f"   [bold bright_green][✓][/bold bright_green] "
        f"[white]Account for {email} successfully hashed and committed![/white]"
    )
    console.print("   [dim]Press Enter to return...[/dim]", end="")
    get_text_input("", default_val="")

async def reset_user_password():
    clear_screen()
    
    console.print(Panel("[bold white]🔑   RESET USER PASSWORD[/bold white]", border_style="bright_magenta", width=68))
    console.print()
    
    engine = create_async_engine(settings.DATABASE_URL)
    async with AsyncSession(engine) as session:
        statement = select(User)
        result = await session.exec(statement)
        user = result.first()
        if not user:
            console.print("\n   [bold bright_red][✗][/bold bright_red] [white]No registered user account found![/white]")
            console.print("   [dim]Press Enter to return...[/dim]", end="")
            get_text_input("", default_val="")
            return
            
        password = prompt_input("Enter New Password", is_masked=True)
        if password == "ESC" or len(password) < 6:
            console.print("\n   [bold bright_red][✗][/bold bright_red] [white]Password too short or operation aborted.[/white]")
            console.print("   [dim]Press Enter to return...[/dim]", end="")
            get_text_input("", default_val="")
            return
            
        hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        user.password_hash = hashed
        session.add(user)
        await session.commit()
        
    console.print("\n   [bold bright_green][✓][/bold bright_green] [white]Password updated successfully![/white]")
    console.print("   [dim]Press Enter to return...[/dim]", end="")
    get_text_input("", default_val="")

async def configure_user_2fa():
    clear_screen()
    
    console.print(Panel("[bold white]🛡️   CONFIGURE 2FA (TOTP) SETTINGS[/bold white]", border_style="bright_magenta", width=68))
    console.print()
    
    engine = create_async_engine(settings.DATABASE_URL)
    async with AsyncSession(engine) as session:
        statement = select(User)
        result = await session.exec(statement)
        user = result.first()
        if not user:
            console.print("\n   [bold bright_red][✗][/bold bright_red] [white]No registered user account found![/white]")
            console.print("   [dim]Press Enter to return...[/dim]", end="")
            get_text_input("", default_val="")
            return
            
        if user.two_factor_enabled:
            confirm = prompt_input(f"2FA is currently ENABLED for {user.email}. Disable it? (y/N)", "N")
            if confirm.lower() == "y" and confirm != "ESC":
                user.two_factor_enabled = False
                user.totp_secret = None
                session.add(user)
                await session.commit()
                console.print("\n   [bold bright_green][✓][/bold bright_green] [white]2FA has been disabled.[/white]")
            else:
                console.print("\n   [bold bright_yellow][!][/bold bright_yellow] [dim]No changes made.[/dim]")
        else:
            confirm = prompt_input(f"2FA is currently DISABLED for {user.email}. Enable it? (y/N)", "N")
            if confirm.lower() == "y" and confirm != "ESC":
                secret = pyotp.random_base32()
                totp = pyotp.TOTP(secret)
                prov_uri = totp.provisioning_uri(name=user.email, issuer_name="StreamHome")
                
                console.print(f"\n   [bold cyan]TOTP Secret Key:[/bold cyan] [bold white]{secret}[/bold white]")
                console.print(f"   [bold cyan]Provisioning URI:[/bold cyan] [dim]{prov_uri}[/dim]\n")
                console.print("   [bold bright_yellow]![/bold bright_yellow] Scan the QR code or type this key into your authenticator app.")
                
                code = prompt_input("Enter current 6-digit verification code to confirm")
                if code != "ESC" and totp.verify(code):
                    user.totp_secret = protect_secret(secret)
                    user.two_factor_enabled = True
                    session.add(user)
                    await session.commit()
                    console.print("\n   [bold bright_green][✓][/bold bright_green] [white]2FA successfully enabled for this account![/white]")
                else:
                    console.print("\n   [bold bright_red][✗][/bold bright_red] [white]Invalid code. Setup aborted.[/white]")
            else:
                console.print("\n   [bold bright_yellow][!][/bold bright_yellow] [dim]Setup cancelled.[/dim]")
                
    console.print("   [dim]Press Enter to return...[/dim]", end="")
    get_text_input("", default_val="")

async def manage_account_and_security():
    sub_options = [
        "Register New User Account",
        "Reset User Password",
        "Configure 2FA Options",
        "Back to Main Menu"
    ]
    sub_icons = ["👤", "🔑", "🛡️", "↩️"]
    while True:
        choice = arrow_menu(sub_options, sub_icons, is_sub_menu=True)
        if choice == -1 or choice == 3:
            return
        elif choice == 0:
            await register_user()
        elif choice == 1:
            await reset_user_password()
        elif choice == 2:
            await configure_user_2fa()

# ─────────────────────── 3. Monitor Downloads ───────────────────────

async def monitor_downloads():
    """Display active download queue status from the database. Supports ESC instant return with 1s auto-refresh."""
    engine = create_async_engine(settings.DATABASE_URL)
    
    # Disable terminal echo on Unix/Linux to prevent keypress pollution
    old_settings = None
    fd = None
    if sys.platform != "win32":
        try:
            import termios
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            new_settings = termios.tcgetattr(fd)
            new_settings[3] = new_settings[3] & ~termios.ECHO & ~termios.ICANON
            termios.tcsetattr(fd, termios.TCSANOW, new_settings)
        except Exception:
            pass

    last_state = None
    try:
        while True:
            try:
                async with AsyncSession(engine) as session:
                    stmt = select(DownloadTask).order_by(DownloadTask.created_at.desc())
                    result = await session.exec(stmt)
                    tasks = result.all()
            except Exception as e:
                console.print(f"   [bold bright_red][✗][/bold bright_red] [white]Failed to query database: {e}[/white]")
                console.print("   [dim]Press Enter to return to Main Menu...[/dim]", end="")
                get_text_input("", default_val="")
                return
            
            # Load transient active metrics from the temporary JSON file
            metrics = {}
            try:
                metrics_file = os.path.join(config_dir, "temp", "download_metrics.json")
                if os.path.exists(metrics_file):
                    with open(metrics_file, "r") as f:
                        metrics = json.load(f)
            except Exception:
                pass
            
            # Build current state signature to detect changes
            current_state = []
            for t in tasks[:20]:
                task_metrics = None
                if t.status in ("DOWNLOADING", "MERGING", "MOVING_CLOUD") and str(t.id) in metrics:
                    m = metrics[str(t.id)]
                    task_metrics = (m.get("progress"), m.get("speed"), m.get("eta"), m.get("size"))
                current_state.append((
                    t.id, t.status, t.error_message, t.title, t.media_type, t.quality, t.created_at, task_metrics
                ))
            
            # Redraw only if the state has changed
            if current_state != last_state:
                last_state = current_state
                
                clear_screen()
                
                console.print(Panel(
                    "[bold white]📊   ACTIVE DOWNLOAD QUEUE & WORKERS (Auto-refreshing...)[/bold white]\n"
                    "[dim italic]Press ESC, Q or Enter at any time to return to Main Menu[/dim italic]",
                    border_style="bright_blue",
                    width=68,
                    padding=(0, 2)
                ))
                console.print()
                
                if not tasks:
                    console.print("   [dim]No download tasks found in the queue.[/dim]")
                else:
                    table = Table(
                        show_header=True,
                        header_style="bold bright_cyan",
                        border_style="dim",
                        width=68,
                        pad_edge=True,
                    )
                    table.add_column("Title", style="white", max_width=26, no_wrap=True)
                    table.add_column("Type", style="dim", width=6, justify="center")
                    table.add_column("Quality", style="dim", width=7, justify="center")
                    table.add_column("Status", width=25)
                    table.add_column("Created", style="dim", width=11, justify="right")
                    
                    status_labels = {
                        "COMPLETED":    "[bold bright_green]✓ Done[/bold bright_green]",
                        "FAILED":       "[bold bright_red]✗ Failed[/bold bright_red]",
                        "DOWNLOADING":  "[bold bright_yellow]↓ Downloading[/bold bright_yellow]",
                        "PENDING":      "[dim]⧗ Pending[/dim]",
                        "MERGING":      "[bold bright_cyan]⚙ Merging[/bold bright_cyan]",
                        "MOVING_CLOUD": "[bold bright_magenta]☁ Uploading[/bold bright_magenta]",
                    }
                    
                    for t in tasks[:20]:
                        label = status_labels.get(t.status, f"[dim]{t.status}[/dim]")
                        
                        # Enhance status label dynamically if active metrics exist
                        if t.status in ("DOWNLOADING", "MERGING", "MOVING_CLOUD") and str(t.id) in metrics:
                            task_metrics = metrics[str(t.id)]
                            progress_val = task_metrics.get("progress", 0.0)
                            speed_val = task_metrics.get("speed", "0 Mbps")
                            eta_val = task_metrics.get("eta", "00:00:00")
                            size_val = task_metrics.get("size", "0 MB")
                            
                            if t.status == "DOWNLOADING":
                                label = (
                                    f"[bold bright_yellow]↓ Downloading ({progress_val}%)[/bold bright_yellow]\n"
                                    f"[dim]{size_val} @ {speed_val}[/dim]\n"
                                    f"[dim]ETA: {eta_val}[/dim]"
                                )
                            elif t.status == "MERGING":
                                label = (
                                    f"[bold bright_cyan]⚙ Merging ({progress_val}%)[/bold bright_cyan]\n"
                                    f"[dim]{size_val} @ {speed_val}[/dim]\n"
                                    f"[dim]ETA: {eta_val}[/dim]"
                                )
                            elif t.status == "MOVING_CLOUD":
                                label = (
                                    f"[bold bright_magenta]☁ Uploading ({progress_val}%)[/bold bright_magenta]\n"
                                    f"[dim]{size_val} @ {speed_val}[/dim]\n"
                                    f"[dim]ETA: {eta_val}[/dim]"
                                )
                        elif t.status == "FAILED" and t.error_message:
                            err_clean = t.error_message.replace("\n", " ").strip()
                            if len(err_clean) > 28:
                                err_clean = err_clean[:25] + "..."
                            label = f"[bold bright_red]✗ Failed[/bold bright_red]\n[dim red]{err_clean}[/dim red]"
                        
                        title_display = (t.title or f"TMDB {t.tmdb_id}")[:26]
                        created_display = t.created_at[:10] if t.created_at else "—"
                        
                        table.add_row(
                            title_display,
                            t.media_type or "—",
                            t.quality or "Source",
                            label,
                            created_display,
                        )
                    
                    console.print(table)
                    
                    # Summary counters
                    total = len(tasks)
                    completed = sum(1 for t in tasks if t.status == "COMPLETED")
                    failed = sum(1 for t in tasks if t.status == "FAILED")
                    active = sum(1 for t in tasks if t.status in ("DOWNLOADING", "MERGING", "MOVING_CLOUD"))
                    pending = sum(1 for t in tasks if t.status == "PENDING")
                    
                    console.print()
                    console.print(
                        f"   [dim]Total: {total}  ·  Active: {active}  ·  "
                        f"Pending: {pending}  ·  Completed: {completed}  ·  Failed: {failed}[/dim]"
                    )
                
                console.print()
                console.print("   [dim]Press ESC, Q or Enter to return to Main Menu...[/dim]", end="")
                sys.stdout.flush()
            
            # Poll kbhit every 50ms up to 20 times (1 second cooldown) to remain responsive to exits
            user_exited = False
            for _ in range(20):
                if kbhit():
                    k = get_key()
                    if k in ("ENTER", "ESC") or (isinstance(k, str) and k.lower() in ("q", "x")):
                        user_exited = True
                        break
                await asyncio.sleep(0.05)
                
            if user_exited:
                break
    finally:
        # Restore terminal settings on Unix/Linux
        if old_settings is not None and fd is not None:
            try:
                import termios
                termios.tcsetattr(fd, termios.TCSANOW, old_settings)
            except Exception:
                pass

# ─────────────────────── 4. Remove Media Asset ───────────────────────

async def remove_media_asset():
    """Interactive media asset deletion portal with database and filesystem removal."""
    clear_screen()
    console.print(Panel(
        "[bold white]🗑️   REMOVE MEDIA ASSET PORTAL[/bold white]\n"
        "[dim italic]Completely delete media from database and disk[/dim italic]",
        border_style="bright_red",
        width=68,
        padding=(0, 2)
    ))
    console.print()
    
    engine = create_async_engine(settings.DATABASE_URL)
    async with AsyncSession(engine) as session:
        stmt = select(Movie).order_by(Movie.title)
        result = await session.exec(stmt)
        movies = result.all()
        
    if not movies:
        console.print("   [dim]No media assets found in the database.[/dim]\n")
        console.print("   [dim]Press Enter to return to Main Menu...[/dim]", end="")
        get_text_input("", default_val="")
        return
        
    options = [f"{m.title} ({m.release_year}) [{m.type.upper()}]" for m in movies]
    options.append("Back to Main Menu")
    icons = ["🎬" if m.type == "movie" else "📺" for m in movies]
    icons.append("↩️")
    
    while True:
        choice = arrow_menu(options, icons, is_sub_menu=True)
        if choice == -1 or choice == len(movies):
            return
            
        selected_movie = movies[choice]
        clear_screen()
        
        console.print(Panel(
            f"[bold white]⚠️   CONFIRM PERMANENT DELETION[/bold white]\n"
            f"[dim]Asset: [bold]{selected_movie.title}[/bold] ({selected_movie.release_year})[/dim]",
            border_style="bright_red",
            width=68,
            padding=(0, 2)
        ))
        console.print()
        console.print(f"   [bold bright_red][WARNING][/bold bright_red] This action is irreversible!")
        console.print("   This will permanently delete:")
        console.print(f"   [bold]- The database entry for this {selected_movie.type}[/bold]")
        if selected_movie.type == "series":
            console.print("   [bold]- All database episodes linked to this series[/bold]")
        console.print("   [bold]- The entire physical media folder and all files on disk[/bold]\n")
        
        confirm = prompt_input("Are you sure you want to proceed? (yes/No)", "No")
        if confirm.lower() == "yes" and confirm != "ESC":
            # 1. Resolve disk path
            folder_name = None
            media_sub_dir = "Movies" if selected_movie.type == "movie" else "Series"
            
            # Try to resolve folder name from database values
            if selected_movie.type == "movie" and selected_movie.video_url:
                parts = selected_movie.video_url.lstrip("/").split("/")
                if len(parts) >= 3 and parts[1] == "Movies":
                    folder_name = parts[2]
            elif selected_movie.type == "series":
                # Find episodes to get their directory path
                async with AsyncSession(engine) as session:
                    stmt = select(Episode).where(Episode.movie_id == selected_movie.id)
                    result = await session.exec(stmt)
                    episodes = result.all()
                if episodes:
                    for ep in episodes:
                        if ep.video_url:
                            parts = ep.video_url.lstrip("/").split("/")
                            if len(parts) >= 3 and parts[1] == "Series":
                                folder_name = parts[2]
                                break
                                
            # Fallback to name-based construction if video_url wasn't set or parsed
            if not folder_name:
                clean_title = "".join(c for c in selected_movie.title if c.isalnum() or c in " .-_")
                if selected_movie.type == "movie":
                    folder_name = f"{clean_title}_{selected_movie.release_year}_TMDB_{selected_movie.id.replace('m_', '')}"
                else:
                    folder_name = f"{clean_title}_TMDB_{selected_movie.id.replace('tv_', '')}"
                    
            config_dir = os.path.dirname(os.path.abspath(__file__))
            disk_path = os.path.abspath(os.path.join(config_dir, "media", media_sub_dir, folder_name))
            
            console.print(f"\n   [cyan]Physical Folder Path:[/cyan] {disk_path}")
            
            # 2. Perform DB deletion
            async with AsyncSession(engine) as session:
                # If series, delete all episodes
                if selected_movie.type == "series":
                    stmt_eps = select(Episode).where(Episode.movie_id == selected_movie.id)
                    res_eps = await session.exec(stmt_eps)
                    eps_to_delete = res_eps.all()
                    for ep in eps_to_delete:
                        await session.delete(ep)
                        
                # Delete the movie record
                stmt_movie = select(Movie).where(Movie.id == selected_movie.id)
                res_movie = await session.exec(stmt_movie)
                movie_to_delete = res_movie.first()
                if movie_to_delete:
                    await session.delete(movie_to_delete)
                    
                await session.commit()
                console.print("   [bold bright_green][✓][/bold bright_green] Database entries removed successfully.")
                
            # 3. Perform File deletion
            if os.path.exists(disk_path):
                try:
                    shutil.rmtree(disk_path)
                    console.print("   [bold bright_green][✓][/bold bright_green] Physical files deleted from disk.")
                except Exception as e:
                    console.print(f"   [bold bright_red][✗][/bold bright_red] Failed to delete disk files: {e}")
            else:
                console.print("   [bold bright_yellow][!][/bold bright_yellow] Media folder did not exist on disk (nothing to clean up).")
                
            console.print("\n   [bold bright_green][SUCCESS][/bold bright_green] Media asset deleted entirely!")
            console.print("   [dim]Press Enter to continue...[/dim]", end="")
            get_text_input("", default_val="")
            return
        else:
            console.print("\n   [bold bright_yellow][!][/bold bright_yellow] Deletion cancelled.")
            console.print("   [dim]Press Enter to return...[/dim]", end="")
            get_text_input("", default_val="")

# ─────────────────────── Setup Wizard & Main ───────────────────────

BANNER = """
    [bold red]



        ███████ ████████ ██████  ███████  █████  ███    ███ ██   ██  ██████  ███    ███ ███████ 
        ██         ██    ██   ██ ██      ██   ██ ████  ████ ██   ██ ██    ██ ████  ████ ██      
        ███████    ██    ██████  █████   ███████ ██ ████ ██ ███████ ██    ██ ██ ████ ██ █████   
             ██    ██    ██   ██ ██      ██   ██ ██  ██  ██ ██   ██ ██    ██ ██  ██  ██ ██      
        ███████    ██    ██   ██ ███████ ██   ██ ██      ██ ██   ██  ██████  ██      ██ ███████
    [/bold red]
"""

LITTLEBANNER = """
[bold red]



        ▄█████ ██████ █████▄  ██████ ▄████▄ ██▄  ▄██ ██  ██ ▄████▄ ██▄  ▄██ ██████ 
        ▀▀▀▄▄▄   ██   ██▄▄██▄ ██▄▄   ██▄▄██ ██ ▀▀ ██ ██████ ██  ██ ██ ▀▀ ██ ██▄▄   
        █████▀   ██   ██   ██ ██▄▄▄▄ ██  ██ ██    ██ ██  ██ ▀████▀ ██    ██ ██▄▄▄▄ 
[/bold red]"""
def print_centered(text, end="\n"):
    # Strip rich formatting tags to find true text length
    clean_text = re.sub(r"\[/?.*?\]", "", text)
    term_width = console.width
    padding = max(0, (term_width - len(clean_text)) // 2)
    console.print(" " * padding + text, end=end)

def print_centered_block(block_text):
    for line in block_text.splitlines():
        line_clean = line.replace("[bold red]", "").replace("[/bold red]", "")
        if line_clean.strip():
            print_centered(f"[bold red]{line_clean}[/bold red]")

async def run_setup_wizard():
    def confirm_abort():
        import time
        print_centered("[bold bright_red]Are you sure? Press ESC again to abort setup, or any other key to continue...[/bold bright_red]")
        time.sleep(0.3)
        while kbhit():
            try:
                getch()
            except Exception:
                pass
        key = get_key()
        return key == "ESC"

    clear_screen()
    console.print(Align.center(BANNER))
    console.print()
    console.print(Align.center("[bold white]STREAMHOME INSTALLATION & SETUP WIZARD[/bold white]"))
    console.print(Align.center("─" * 68))
    console.print(
        Align.center(
            "Welcome to the StreamHome Self-Hosted Media Server setup wizard!\n"
            "This program will guide you through setting up your database,\n"
            "creating your administrator account, configuring TMDB secrets, and\n"
            "setting up storage parameters.\n\n\n"
            "                              [[bold white]Continue[/bold white]]"
        )
    )
    
    # Wait for enter or ESC
    while True:
        key = get_key()
        if key == "ESC":
            if confirm_abort():
                console.print("\n   [bold bright_red][✗][/bold bright_red] [white]Setup aborted.[/white]\n")
                return
            else:
                clear_screen()
                console.print(Align.center(BANNER))
                console.print()
                console.print(Align.center("[bold white]STREAMHOME INSTALLATION & SETUP WIZARD[/bold white]"))
                console.print(Align.center("─" * 68))
                console.print(
                    Align.center(
                        "Welcome to the StreamHome Self-Hosted Media Server setup wizard!\n"
                        "This program will guide you through setting up your database,\n"
                        "creating your administrator account, configuring TMDB secrets, and\n"
                        "setting up storage parameters.\n\n\n"
                        "                              [[bold white]Continue[/bold white]]"
                    )
                )
        elif key == "ENTER":
            break

    # State variables
    state = {
        "email": "",
        "password": "",
        "2fa_enabled": None,  # True/False
        "totp_secret": None,
        "tmdb_token": "",
        "storage_mode": "",
        "rclone_path": "",
        "backup_enabled": None,  # True/False
        "auto_update_enabled": None,  # True/False
    }

    # Dynamic render helper
    def draw_wizard_state(active_step, active_field=None):
        clear_screen()
        console.print()
        console.print()
        print_centered_block(LITTLEBANNER)
        console.print()
        print_centered("[bold white]STREAMHOME INSTALLATION & SETUP WIZARD[/bold white]")
        print_centered("─" * 68)
        console.print()

        # Step 1: Create Admin Account
        if active_step == 1:
            print_centered("[bold magenta][▶] Step 1: Create Administrator Account[/bold magenta]")
            if state["email"]:
                print_centered(f"      Email Address: {state['email']}")
            elif active_field == "email":
                print_centered("      > Email Address: [ ", end="")
                return

            if state["password"]:
                print_centered("      Password: ********")
            elif active_field == "password":
                print_centered("      > Password: [ ", end="")
                return

            if state["2fa_enabled"] is not None and active_field != "2fa":
                print_centered(f"      Enable 2FA (y/N): {'Y' if state['2fa_enabled'] else 'N'}")
            elif active_field == "2fa":
                print_centered("      > Enable 2FA (y/N): [ ", end="")
                return

            if state["2fa_enabled"] and active_field == "2fa_code":
                print_centered("      > Enter 2FA Verification Code: [ ", end="")
                return
        elif active_step > 1:
            print_centered(f"[green][✓] Step 1: Create Administrator Account ({state['email']})[/green]")
        else:
            print_centered("[dim][ ] Step 1: Create Administrator Account[/dim]")

        console.print()

        # Step 2: TMDB Access
        if active_step == 2:
            print_centered("[bold yellow][▶] Step 2: Configure TMDB Access[/bold yellow]")
            if state["tmdb_token"]:
                print_centered("      TMDB Token: ********")
            elif active_field == "tmdb_token":
                print_centered("      > TMDB Read Access Token (JWT): [ ", end="")
                return
        elif active_step > 2:
            val_str = "Token Configured" if state["tmdb_token"] else "Skipped"
            print_centered(f"[green][✓] Step 2: Configure TMDB Access ({val_str})[/green]")
        else:
            print_centered("[dim][ ] Step 2: Configure TMDB Access[/dim]")

        console.print()

        # Step 3: Storage & Backup Settings
        if active_step == 3:
            print_centered("[bold blue][▶] Step 3: Choose Storage & Backup Settings[/bold blue]")
            if state["storage_mode"] and active_field != "storage_mode":
                print_centered(f"      Storage Engine Mode: {state['storage_mode']}")
            elif active_field == "storage_mode":
                print_centered("      > Storage Engine Mode (LOCAL/CLOUD): [ ", end="")
                return

            if state["storage_mode"] == "CLOUD":
                if state["rclone_path"] and active_field != "rclone_path":
                    print_centered(f"      Rclone Remote Path: {state['rclone_path']}")
                elif active_field == "rclone_path":
                    default_remote = get_default_rclone_remote()
                    print_centered(f"      > Rclone Remote Path (Default: {default_remote}): [ ", end="")
                    return

            if state["backup_enabled"] is not None and active_field != "backup_enabled":
                print_centered(f"      Enable Automated Backups (y/N): {'Y' if state['backup_enabled'] else 'N'}")
            elif active_field == "backup_enabled":
                print_centered("      > Enable Automated Backups (y/N): [ ", end="")
                return

            if state["auto_update_enabled"] is not None and active_field != "auto_update_enabled":
                print_centered(f"      Enable Automated Updates (y/N): {'Y' if state['auto_update_enabled'] else 'N'}")
            elif active_field == "auto_update_enabled":
                print_centered("      > Enable Automated Updates (y/N): [ ", end="")
                return
        elif active_step > 3:
            print_centered(f"[green][✓] Step 3: Choose Storage & Backup Settings ({state['storage_mode']}, Backup: {'On' if state['backup_enabled'] else 'Off'}, Updates: {'On' if state['auto_update_enabled'] else 'Off'})[/green]")
        else:
            print_centered("[dim][ ] Step 3: Choose Storage & Backup Settings[/dim]")

        console.print()

        # Step 4: Finalize
        if active_step == 4:
            print_centered("[bold green][▶] Step 4: Finalize Setup[/bold green]")
        else:
            print_centered("[dim][ ] Step 4: Finalize Setup[/dim]")

        console.print()


    # Step transition state machine
    step = 0
    while step < 9:
        if step == 0:
            draw_wizard_state(1, "email")
            email = get_inline_input().strip()
            if email == "ESC":
                if confirm_abort():
                    console.print("\n   [bold bright_red][✗][/bold bright_red] [white]Setup aborted.[/white]\n")
                    return
                continue
            if not email or "@" not in email:
                print_centered("[bold bright_red]Please enter a valid email address.[/bold bright_red]")
                await asyncio.sleep(1.5)
                continue
            state["email"] = email
            step = 1

        elif step == 1:
            draw_wizard_state(1, "password")
            password = get_inline_input(is_masked=True).strip()
            if password == "ESC":
                step = 0
                continue
            if len(password) < 6:
                print_centered("[bold bright_red]Password must be at least 6 characters long.[/bold bright_red]")
                await asyncio.sleep(1.5)
                continue
            state["password"] = password
            step = 2

        elif step == 2:
            draw_wizard_state(1, "2fa")
            confirm_2fa = get_inline_input()
            if confirm_2fa == "ESC":
                step = 1
                continue
            if not confirm_2fa:
                confirm_2fa = "N"
            if confirm_2fa.lower() in ["y", "yes", "n", "no"]:
                state["2fa_enabled"] = confirm_2fa.lower() in ["y", "yes"]
                if state["2fa_enabled"]:
                    step = 3
                else:
                    step = 4
            else:
                print_centered("[bold bright_red]Please enter Y or N.[/bold bright_red]")
                await asyncio.sleep(1.5)

        elif step == 3:
            secret = pyotp.random_base32()
            totp = pyotp.TOTP(secret)
            prov_uri = totp.provisioning_uri(name=state["email"], issuer_name="StreamHome")
            
            clear_screen()
            print_centered_block(LITTLEBANNER)
            console.print()
            print_centered("[bold white]🛡️   TWO-FACTOR AUTHENTICATION SETUP[/bold white]")
            print_centered("─" * 68)
            console.print()
            print_centered(f"TOTP Secret Key: [bold white]{secret}[/bold white]")
            print_centered(f"Provisioning URI: [dim]{prov_uri}[/dim]")
            print_centered("Add this secret key to your authenticator app (Google Authenticator, Authy, etc.).")
            console.print()
            
            draw_wizard_state(1, "2fa_code")
            code = get_inline_input().strip()
            if code == "ESC":
                state["2fa_enabled"] = False
                step = 2
                continue
            if totp.verify(code):
                print_centered("[bold green]2FA successfully validated and enabled![/bold green]")
                state["totp_secret"] = secret
                await asyncio.sleep(1.5)
                step = 4
            else:
                print_centered("[bold bright_red]Invalid verification code. Try again.[/bold bright_red]")
                await asyncio.sleep(1.5)

        elif step == 4:
            draw_wizard_state(2, "tmdb_token")
            token = get_inline_input().strip()
            if token == "ESC":
                if state["2fa_enabled"]:
                    step = 3
                else:
                    step = 2
                continue
            if not token:
                state["tmdb_token"] = ""
                step = 5
                continue
                
            print_centered("Validating TMDB Read Access Token with Movie ID 290250...")
            try:
                headers = {"Authorization": f"Bearer {token}"}
                response = httpx.get("https://api.themoviedb.org/3/movie/290250", headers=headers, timeout=10.0)
                if response.status_code == 200:
                    data = response.json()
                    movie_title = data.get("title", "Unknown")
                    print_centered(f"[bold green]Token is valid! Verified movie: '{movie_title}'[/bold green]")
                    state["tmdb_token"] = token
                    await asyncio.sleep(1.5)
                    step = 5
                else:
                    print_centered(f"[bold bright_red]Validation failed. TMDB API returned status: {response.status_code}[/bold bright_red]")
                    await asyncio.sleep(1.5)
            except Exception as e:
                print_centered(f"[bold bright_red]Connection error validating TMDB token: {e}[/bold bright_red]")
                await asyncio.sleep(1.5)

        elif step == 5:
            draw_wizard_state(3, "storage_mode")
            mode = get_inline_input().strip()
            if mode == "ESC":
                step = 4
                continue
            if not mode:
                mode = "LOCAL"
            if mode.upper() in ["LOCAL", "CLOUD"]:
                state["storage_mode"] = mode.upper()
                if mode.upper() == "CLOUD":
                    step = 6
                else:
                    step = 7
            else:
                print_centered("[bold bright_red]Invalid mode. Please type LOCAL or CLOUD.[/bold bright_red]")
                await asyncio.sleep(1.5)

        elif step == 6:
            draw_wizard_state(3, "rclone_path")
            path = get_inline_input().strip()
            if path == "ESC":
                step = 5
                continue
            if not path.strip():
                path = get_default_rclone_remote()
            
            if path.strip():
                state["rclone_path"] = path.strip()
                step = 7
            else:
                print_centered("[bold bright_red]Rclone Remote Path cannot be empty.[/bold bright_red]")
                await asyncio.sleep(1.5)

        elif step == 7:
            draw_wizard_state(3, "backup_enabled")
            confirm_backup = get_inline_input().strip()
            if confirm_backup == "ESC":
                if state["storage_mode"] == "CLOUD":
                    step = 6
                else:
                    step = 5
                continue
            if not confirm_backup:
                confirm_backup = "N"
            if confirm_backup.lower() in ["y", "yes", "n", "no"]:
                state["backup_enabled"] = confirm_backup.lower() in ["y", "yes"]
                step = 8
            else:
                print_centered("[bold bright_red]Please enter Y or N.[/bold bright_red]")
                await asyncio.sleep(1.5)

        elif step == 8:
            draw_wizard_state(3, "auto_update_enabled")
            confirm_update = get_inline_input().strip()
            if confirm_update == "ESC":
                step = 7
                continue
            if not confirm_update:
                confirm_update = "N"
            if confirm_update.lower() in ["y", "yes", "n", "no"]:
                state["auto_update_enabled"] = confirm_update.lower() in ["y", "yes"]
                step = 9
            else:
                print_centered("[bold bright_red]Please enter Y or N.[/bold bright_red]")
                await asyncio.sleep(1.5)

    # Save to database
    hashed = bcrypt.hashpw(state["password"].encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    engine = create_async_engine(settings.DATABASE_URL)
    async with AsyncSession(engine) as session:
        statement = select(User).where(User.email == state["email"])
        result = await session.exec(statement)
        existing_user = result.first()
        if existing_user:
            existing_user.password_hash = hashed
            existing_user.two_factor_enabled = state["2fa_enabled"]
            existing_user.totp_secret = protect_secret(state["totp_secret"]) if state["2fa_enabled"] else None
            session.add(existing_user)
        else:
            new_user = User(
                email=state["email"],
                password_hash=hashed,
                two_factor_enabled=state["2fa_enabled"],
                totp_secret=protect_secret(state["totp_secret"]) if state["2fa_enabled"] else None
            )
            session.add(new_user)
        await session.commit()

    if state["tmdb_token"]:
        update_env_file("TMDB_READ_ACCESS_TOKEN", state["tmdb_token"])
        settings.TMDB_READ_ACCESS_TOKEN = state["tmdb_token"]
    update_env_file("STORAGE_ENGINE", state["storage_mode"])
    settings.STORAGE_ENGINE = state["storage_mode"]
    if state["storage_mode"] == "CLOUD":
        update_env_file("RCLONE_REMOTE_PATH", state["rclone_path"])
        settings.RCLONE_REMOTE_PATH = state["rclone_path"]
    update_env_file("BACKUP_ENABLED", str(state["backup_enabled"]))
    settings.BACKUP_ENABLED = state["backup_enabled"]
    update_env_file("AUTO_UPDATE_ENABLED", str(state["auto_update_enabled"]))
    settings.AUTO_UPDATE_ENABLED = state["auto_update_enabled"]

    settings.save_to_json()

    # Step 4: Finalize
    draw_wizard_state(4)
    console.print()
    clear_screen()
    console.print()
    console.print()
    console.print()
    console.print()
    console.print()
    console.print()
    print_centered("[bold bright_green]🎉   STREAMHOME SETUP COMPLETE![/bold bright_green]")
    print_centered("─" * 68)
    print_centered("StreamHome has been configured and initialized successfully!\n")
    print_centered("[bold white]To run StreamHome in the foreground:[/bold white]")
    print_centered("  - Windows : Run start.bat")
    print_centered("  - Linux   : Run ./start.sh\n")
    print_centered("[bold white]To run StreamHome in the background:[/bold white]")
    print_centered("  - Windows : Run start_background.bat")
    print_centered("  - Linux   : Run ./start_background.sh\n")
    print_centered("[bold white]To open the management panel at any time, run:[/bold white]")
    print_centered("[bold bright_cyan]python server\\cli.py[/bold bright_cyan]")
    console.print()
    print_centered("Press Enter to exit.")
    get_text_input("", default_val="")

async def manage_database_backups():
    from services.backup import (
        create_backup,
        prune_old_backups,
        get_local_backups,
        sync_backups_to_cloud,
        restore_backup
    )
    
    sub_options = [
        "Create Database Backup & Sync to Cloud",
        "List & Manage Local Backups",
        "Restore Database from Backup",
        "Delete Local Backup File",
        "Back to Main Menu"
    ]
    sub_icons = ["✨", "📋", "↩️", "🗑️", "❌"]
    
    while True:
        choice = arrow_menu(sub_options, sub_icons, is_sub_menu=True)
        
        if choice == -1 or choice == 4:  # ESC or Back to Main Menu
            return
            
        elif choice == 0:
            # Create Backup & Sync to Cloud
            clear_screen()
            console.print(f"[bright_cyan]{'═' * console.width}[/bright_cyan]")
            print_centered_block(LITTLEBANNER)
            console.print()
            print_centered("[bold white]💾   CREATE DATABASE BACKUP[/bold white]")
            print_centered("─" * 68)
            console.print()
            
            print_centered("Creating secure database online backup...")
            try:
                backup_path = await create_backup()
                prune_old_backups(keep_count=7)
                print_centered(f"[bold green][✓] Backup created locally: {os.path.basename(backup_path)}[/bold green]")
                
                if settings.STORAGE_ENGINE == "CLOUD":
                    print_centered("Synchronizing backups to the cloud...")
                    sync_success = await sync_backups_to_cloud()
                    if sync_success:
                        print_centered("[bold green][✓] Cloud synchronization completed successfully![/bold green]")
                    else:
                        print_centered("[bold bright_red][✗] Cloud synchronization failed.[/bold bright_red]")
            except Exception as e:
                print_centered(f"[bold bright_red][✗] Backup failed: {e}[/bold bright_red]")
                
            console.print()
            print_centered("[dim]Press Enter to continue...[/dim]")
            get_text_input("", default_val="")
            
        elif choice == 1:
            # List Local Backups
            while True:
                clear_screen()
                console.print(f"[bright_cyan]{'═' * console.width}[/bright_cyan]")
                print_centered_block(LITTLEBANNER)
                console.print()
                print_centered("[bold white]📋   LOCAL DATABASE BACKUPS[/bold white]")
                print_centered("─" * 68)
                console.print()
                
                backups = get_local_backups()
                if not backups:
                    print_centered("[dim]No database backups found inside server/backup/.[/dim]")
                else:
                    for idx, b in enumerate(backups):
                        print_centered(f"[{idx+1}] {b['filename']}  ({b['formatted_size']})  -  {b['timestamp'][:19].replace('T', ' ')}")
                
                console.print()
                print_centered("[dim]Press Enter to return...[/dim]")
                k = get_key()
                if k in ("ENTER", "ESC"):
                    break
                    
        elif choice == 2:
            # Restore Database from Backup
            clear_screen()
            console.print(f"[bright_cyan]{'═' * console.width}[/bright_cyan]")
            print_centered_block(LITTLEBANNER)
            console.print()
            print_centered("[bold white]↩️   RESTORE DATABASE FROM BACKUP[/bold white]")
            print_centered("─" * 68)
            console.print()
            
            backups = get_local_backups()
            if not backups:
                print_centered("[dim]No database backups available to restore.[/dim]")
                console.print()
                print_centered("[dim]Press Enter to continue...[/dim]")
                get_text_input("", default_val="")
                continue
                
            print_centered("Select a backup to restore:")
            options_list = [b["filename"] for b in backups] + ["Cancel Restore"]
            icons_list = ["📄"] * len(backups) + ["❌"]
            
            sel = arrow_menu(options_list, icons_list, is_sub_menu=True)
            if sel == -1 or sel == len(backups):
                continue
                
            selected_backup = backups[sel]["filename"]
            
            # Double safety confirmation
            clear_screen()
            console.print(f"[bright_cyan]{'═' * console.width}[/bright_cyan]")
            print_centered_block(LITTLEBANNER)
            console.print()
            print_centered("[bold bright_red]⚠️   WARNING: DATABASE OVERWRITE ACTION[/bold bright_red]")
            print_centered("─" * 68)
            console.print()
            print_centered(f"You are about to restore: [bold white]{selected_backup}[/bold white]")
            print_centered("This will OVERWRITE all current database records and streaming sessions!")
            console.print()
            
            print_centered("[bold yellow]Are you sure? Press ENTER to restore, or ESC to cancel...[/bold yellow]")
            key = get_key()
            if key == "ENTER":
                print_centered("Restoring database...")
                try:
                    success = await restore_backup(selected_backup)
                    if success:
                        print_centered("[bold green][✓] Database successfully restored![/bold green]")
                    else:
                        print_centered("[bold bright_red][✗] Database restore failed.[/bold bright_red]")
                except Exception as e:
                    print_centered(f"[bold bright_red][✗] Restore failed: {e}[/bold bright_red]")
            else:
                print_centered("[dim]Restore cancelled.[/dim]")
                
            console.print()
            print_centered("[dim]Press Enter to continue...[/dim]")
            get_text_input("", default_val="")
            
        elif choice == 3:
            # Delete Backup File
            clear_screen()
            console.print(f"[bright_cyan]{'═' * console.width}[/bright_cyan]")
            print_centered_block(LITTLEBANNER)
            console.print()
            print_centered("[bold white]🗑️   DELETE LOCAL BACKUP FILE[/bold white]")
            print_centered("─" * 68)
            console.print()
            
            backups = get_local_backups()
            if not backups:
                print_centered("[dim]No database backups available to delete.[/dim]")
                console.print()
                print_centered("[dim]Press Enter to continue...[/dim]")
                get_text_input("", default_val="")
                continue
                
            print_centered("Select a backup file to delete:")
            options_list = [b["filename"] for b in backups] + ["Cancel Delete"]
            icons_list = ["📄"] * len(backups) + ["❌"]
            
            sel = arrow_menu(options_list, icons_list, is_sub_menu=True)
            if sel == -1 or sel == len(backups):
                continue
                
            selected_backup = backups[sel]["filename"]
            selected_path = backups[sel]["path"]
            
            clear_screen()
            console.print(f"[bright_cyan]{'═' * console.width}[/bright_cyan]")
            print_centered_block(LITTLEBANNER)
            console.print()
            print_centered(f"Are you sure you want to permanently delete local backup: [bold white]{selected_backup}[/bold white]?")
            console.print()
            print_centered("[bold yellow]Press ENTER to confirm delete, or ESC to cancel...[/bold yellow]")
            key = get_key()
            if key == "ENTER":
                try:
                    os.remove(selected_path)
                    print_centered("[bold green][✓] Backup file successfully deleted.[/bold green]")
                except Exception as e:
                    print_centered(f"[bold bright_red][✗] Deletion failed: {e}[/bold bright_red]")
            else:
                print_centered("[dim]Deletion cancelled.[/dim]")
                
            console.print()
            print_centered("[dim]Press Enter to continue...[/dim]")
            get_text_input("", default_val="")

async def manage_system_updates():
    from services.update import (
        check_for_github_updates,
        is_git_clean,
        get_active_branch,
        pull_and_install_updates,
        self_restart_server,
        is_system_idle
    )
    
    sub_options = [
        "Check for GitHub Updates",
        "Manually Trigger Pull & Apply Updates",
        "Toggle Automated Updates",
        "Back to Main Menu"
    ]
    sub_icons = ["🔍", "🔄", "⚙️", "❌"]
    
    while True:
        choice = arrow_menu(sub_options, sub_icons, is_sub_menu=True)
        
        if choice == -1 or choice == 3:  # ESC or Back to Main Menu
            return
            
        elif choice == 0:
            # Check for GitHub Updates
            clear_screen()
            console.print(f"[bright_cyan]{'═' * console.width}[/bright_cyan]")
            print_centered_block(LITTLEBANNER)
            console.print()
            print_centered("[bold white]🔍   CHECK FOR GITHUB UPDATES[/bold white]")
            print_centered("─" * 68)
            console.print()
            
            print_centered("Checking for updates on GitHub...")
            try:
                update_available = await check_for_github_updates()
                git_clean = await is_git_clean()
                active_branch = await get_active_branch()
                system_idle = await is_system_idle()
                
                print_centered(f"Active Branch: [bold white]{active_branch}[/bold white]")
                if update_available:
                    print_centered("[bold yellow][!] An update is available on GitHub![/bold yellow]")
                else:
                    print_centered("[bold green][✓] StreamHome is already up-to-date.[/bold green]")
                    
                if not git_clean:
                    print_centered("[bold bright_red][⚠️] Warning: Local uncommitted changes detected! Automated updates will be blocked.[/bold bright_red]")
                else:
                    print_centered("[green][✓] Working directory is clean. Updates can be safely applied.[/green]")
                    
                if system_idle:
                    print_centered("[green][✓] System is currently idle.[/green]")
                else:
                    print_centered("[yellow][!] System is currently in use.[/yellow]")
            except Exception as e:
                print_centered(f"[bold bright_red][✗] Update check failed: {e}[/bold bright_red]")
                
            console.print()
            print_centered("[dim]Press Enter to continue...[/dim]")
            get_text_input("", default_val="")
            
        elif choice == 1:
            # Manually Trigger Pull & Apply Updates
            clear_screen()
            console.print(f"[bright_cyan]{'═' * console.width}[/bright_cyan]")
            print_centered_block(LITTLEBANNER)
            console.print()
            print_centered("[bold white]🔄   MANUAL UPDATE PULL & APPLY[/bold white]")
            print_centered("─" * 68)
            console.print()
            
            # 1. Verify working directory is clean
            if not await is_git_clean():
                print_centered("[bold bright_red][✗] Aborting: You have uncommitted changes in your workspace.[/bold bright_red]")
                print_centered("Please commit or stash your changes before updating.")
                console.print()
                print_centered("[dim]Press Enter to continue...[/dim]")
                get_text_input("", default_val="")
                continue
                
            print_centered("Checking remote updates...")
            try:
                update_available = await check_for_github_updates()
                if not update_available:
                    print_centered("[bold green][✓] Already up-to-date. No updates to pull.[/bold green]")
                    console.print()
                    print_centered("[dim]Press Enter to continue...[/dim]")
                    get_text_input("", default_val="")
                    continue
                    
                # Confirm manual trigger
                print_centered("[bold yellow]Are you sure you want to pull updates and restart the server?[/bold yellow]")
                print_centered("[dim]Press ENTER to pull and restart, or ESC to cancel...[/dim]")
                key = get_key()
                if key == "ENTER":
                    print_centered("Pulling updates...")
                    success = await pull_and_install_updates()
                    if success:
                        print_centered("[bold green][✓] Updates successfully applied! Restarting server...[/bold green]")
                        await asyncio.sleep(1.5)
                        self_restart_server()
                        return
                    else:
                        print_centered("[bold bright_red][✗] Failed to apply updates.[/bold bright_red]")
                else:
                    print_centered("[dim]Update pull cancelled.[/dim]")
            except Exception as e:
                print_centered(f"[bold bright_red][✗] Update process failed: {e}[/bold bright_red]")
                
            console.print()
            print_centered("[dim]Press Enter to continue...[/dim]")
            get_text_input("", default_val="")
            
        elif choice == 2:
            # Toggle Automated Updates
            clear_screen()
            console.print(f"[bright_cyan]{'═' * console.width}[/bright_cyan]")
            print_centered_block(LITTLEBANNER)
            console.print()
            print_centered("[bold white]⚙️   TOGGLE AUTOMATED UPDATES[/bold white]")
            print_centered("─" * 68)
            console.print()
            
            status_str = "[bold green]ENABLED[/bold green]" if settings.AUTO_UPDATE_ENABLED else "[bold red]DISABLED[/bold red]"
            print_centered(f"Automated Update System is currently: {status_str}")
            console.print()
            
            prompt = "Disable" if settings.AUTO_UPDATE_ENABLED else "Enable"
            print_centered(f"Do you want to {prompt} automated updates? (y/N): ")
            confirm = get_inline_input()
            if confirm.lower() in ("y", "yes"):
                new_state = not settings.AUTO_UPDATE_ENABLED
                settings.AUTO_UPDATE_ENABLED = new_state
                update_env_file("AUTO_UPDATE_ENABLED", str(new_state))
                settings.save_to_json()
                status_str = "[bold green]ENABLED[/bold green]" if new_state else "[bold red]DISABLED[/bold red]"
                print_centered(f"[bold green][✓] Automated Update System has been {status_str}![/bold green]")
            else:
                print_centered("[dim]No changes made.[/dim]")
                
            console.print()
            print_centered("[dim]Press Enter to continue...[/dim]")
            get_text_input("", default_val="")

async def main():
    await init_db()
    
    if "--setup" in sys.argv:
        await run_setup_wizard()
        return
        
    menu_options = [
        "Configure Environment Server Settings (.env)",
        "Account Management & 2FA Security Center",
        "Monitor Active Download Queue & Workers",
        "Delete / Remove Media Asset from Server",
        "Database Backup & Cloud Sync Center",
        "System Update & Maintenance Center",
        "Exit Control Center",
    ]
    menu_icons = ["⚙️", "👤", "📊", "🗑️", "💾", "🔄", "❌"]
    
    while True:
        choice = arrow_menu(menu_options, menu_icons, is_sub_menu=False)
        
        if choice == 0:
            await configure_settings()
        elif choice == 1:
            await manage_account_and_security()
        elif choice == 2:
            await monitor_downloads()
        elif choice == 3:
            await remove_media_asset()
        elif choice == 4:
            await manage_database_backups()
        elif choice == 5:
            await manage_system_updates()
        elif choice == 6:
            clear_screen()
            console.print()
            console.print(
                "   [bold bright_cyan]✓ [/bold bright_cyan]"
                "[white]Exiting administration console. Goodbye.[/white]"
            )
            console.print()
            break

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print(
            "\n   [bold bright_red][!][/bold bright_red] "
            "[white]Operation cancelled by user.[/white]"
        )
