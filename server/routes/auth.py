import time
import jwt
import bcrypt
import pyotp
from datetime import datetime, timedelta
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, Depends, status, Security, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from db import get_session
from models import User
from config import settings
from services.logger import logger

router = APIRouter(prefix="/api/auth", tags=["Authentication"])
security = HTTPBearer(auto_error=False)

class LoginRequest(BaseModel):
    email: str
    password: str

class VerifyRequest(BaseModel):
    email: str
    code: str

class TOTPVerifySetupRequest(BaseModel):
    code: str

class TOTPDisableRequest(BaseModel):
    code: str

# Helper dependency to authenticate JWT requests
async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: AsyncSession = Depends(get_session)
) -> User:
    token = None
    if credentials:
        token = credentials.credentials
    else:
        token = request.query_params.get("token")

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Missing token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        email = payload.get("sub")
        if not email:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")
        
        statement = select(User).where(User.email == email)
        res = await db.execute(statement)
        user = res.scalars().first()
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
        return user
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

def check_lockout(user: User):
    if user.lockout_until and time.time() < user.lockout_until:
        remaining = int(user.lockout_until - time.time())
        minutes = remaining // 60
        seconds = remaining % 60
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Account is temporarily locked due to too many failed attempts. Try again in {minutes}m {seconds}s."
        )

async def handle_failed_attempt(user: User, db: AsyncSession):
    user.failed_login_attempts += 1
    if user.failed_login_attempts >= 5:
        user.lockout_until = time.time() + 900  # 15 minutes lockout
        logger.warning(f"[Security] Account {user.email} locked out until {datetime.fromtimestamp(user.lockout_until)}")
    db.add(user)
    await db.commit()

@router.post("/login")
async def login(req: LoginRequest, db: AsyncSession = Depends(get_session)):
    statement = select(User).where(User.email == req.email)
    res = await db.execute(statement)
    user = res.scalars().first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
        
    check_lockout(user)
    
    if not bcrypt.checkpw(req.password.encode("utf-8"), user.password_hash.encode("utf-8")):
        await handle_failed_attempt(user, db)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
        
    # Reset attempts on correct password (user has to pass 2FA too, but password is correct)
    user.failed_login_attempts = 0
    user.lockout_until = None
    db.add(user)
    await db.commit()
    
    if user.two_factor_enabled:
        return {
            "requires_2fa": True,
            "email": user.email,
            "message": "TOTP code required to complete login."
        }
        
    # Generate JWT
    payload = {
        "sub": user.email,
        "exp": datetime.utcnow() + timedelta(minutes=settings.JWT_EXPIRATION_MINUTES)
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    
    return {
        "accessToken": token,
        "tokenType": "bearer",
        "email": user.email
    }

@router.post("/verify")
async def verify(req: VerifyRequest, db: AsyncSession = Depends(get_session)):
    statement = select(User).where(User.email == req.email)
    res = await db.execute(statement)
    user = res.scalars().first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
        
    check_lockout(user)
    
    if not user.two_factor_enabled or not user.totp_secret:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="2FA is not enabled for this user")
        
    totp = pyotp.TOTP(user.totp_secret)
    if not totp.verify(req.code):
        await handle_failed_attempt(user, db)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid verification code")
        
    # Success: reset attempts
    user.failed_login_attempts = 0
    user.lockout_until = None
    db.add(user)
    await db.commit()
    
    payload = {
        "sub": user.email,
        "exp": datetime.utcnow() + timedelta(minutes=settings.JWT_EXPIRATION_MINUTES)
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    
    return {
        "accessToken": token,
        "tokenType": "bearer",
        "email": user.email
    }

# ----------------- 2FA TOTP Control Endpoints -----------------

@router.post("/2fa/setup")
async def setup_totp(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    if user.two_factor_enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="2FA is already enabled. Disable it first to reconfigure.")
        
    # Generate TOTP secret and keep in draft on user object
    secret = pyotp.random_base32()
    user.totp_secret = secret
    user.two_factor_enabled = False  # Set to false until verified
    db.add(user)
    await db.commit()
    
    totp = pyotp.TOTP(secret)
    provisioning_uri = totp.provisioning_uri(name=user.email, issuer_name="StreamHome")
    
    return {
        "secret": secret,
        "provisioning_uri": provisioning_uri
    }

@router.post("/2fa/verify-setup")
async def verify_totp_setup(req: TOTPVerifySetupRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    if not user.totp_secret:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="2FA setup has not been initialized.")
        
    totp = pyotp.TOTP(user.totp_secret)
    if not totp.verify(req.code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid verification code. Setup verification failed.")
        
    user.two_factor_enabled = True
    user.failed_login_attempts = 0
    user.lockout_until = None
    db.add(user)
    await db.commit()
    
    logger.info(f"[Security] User {user.email} enabled TOTP 2FA.")
    return {"message": "2FA successfully enabled."}

@router.post("/2fa/disable")
async def disable_totp(req: TOTPDisableRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_session)):
    if not user.two_factor_enabled or not user.totp_secret:
        # Already disabled, return success
        return {"message": "2FA is already disabled."}
        
    totp = pyotp.TOTP(user.totp_secret)
    if not totp.verify(req.code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid verification code. Unable to disable 2FA.")
        
    user.two_factor_enabled = False
    user.totp_secret = None
    db.add(user)
    await db.commit()
    
    logger.info(f"[Security] User {user.email} disabled TOTP 2FA.")
    return {"message": "2FA successfully disabled."}

@router.get("/2fa/status")
async def get_totp_status(user: User = Depends(get_current_user)):
    return {
        "two_factor_enabled": user.two_factor_enabled,
        "email": user.email
    }
