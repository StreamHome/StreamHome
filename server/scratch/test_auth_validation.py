import sys
import os
import jwt
from fastapi import FastAPI, Depends, Request
from fastapi.testclient import TestClient

# Set PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from routes.auth import get_current_user
from config import settings

# Create mock app for testing dependency
app = FastAPI()

@app.get("/test-auth")
async def test_auth_route(user = Depends(get_current_user)):
    return {"status": "authorized", "email": user.email}

# Mock database session to return a dummy user
from sqlmodel.ext.asyncio.session import AsyncSession
from models import User

async def mock_get_session():
    # We will mock the database query to return a dummy user if email matches
    class MockDb:
        async def execute(self, statement):
            class MockResult:
                def scalars(self):
                    class MockScalars:
                        def first(self):
                            return User(email="test@example.com", password_hash="dummy")
                    return MockScalars()
            return MockResult()
    yield MockDb()

# Override get_session dependency
from db import get_session
app.dependency_overrides[get_session] = mock_get_session

def run_tests():
    client = TestClient(app)
    
    print("=== Testing Auth Dependency Checks ===")
    
    # 1. Test unauthenticated request
    res = client.get("/test-auth")
    assert res.status_code == 401, f"Failed: Expected 401, got {res.status_code}"
    print("  [OK] Blocked unauthenticated request correctly (401)")
    
    # Generate valid JWT token
    payload = {"sub": "test@example.com"}
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    
    # 2. Test authenticated request via Header
    res = client.get("/test-auth", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200, f"Failed: Expected 200, got {res.status_code} - {res.text}"
    assert res.json()["email"] == "test@example.com"
    print("  [OK] Allowed authenticated request via Header correctly (200)")
    
    # 3. Test authenticated request via Query Parameter
    res = client.get(f"/test-auth?token={token}")
    assert res.status_code == 200, f"Failed: Expected 200, got {res.status_code} - {res.text}"
    assert res.json()["email"] == "test@example.com"
    print("  [OK] Allowed authenticated request via Query Parameter correctly (200)")
    
    # 4. Test invalid token
    res = client.get("/test-auth", headers={"Authorization": "Bearer invalid-token"})
    assert res.status_code == 401, f"Failed: Expected 401, got {res.status_code}"
    print("  [OK] Blocked invalid token correctly (401)")
    
    print("\n[OK] All core API authentication tests passed successfully!")

if __name__ == "__main__":
    try:
        run_tests()
    except AssertionError as e:
        print(f"\n[ERR] Test assertion failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n[ERR] Test error: {e}")
        sys.exit(1)
