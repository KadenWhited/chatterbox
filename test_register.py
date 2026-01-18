# test_register.py
import requests, sys, json

BASE = "http://127.0.0.1:8000"
USERNAME = "alice"
PASSWORD = "secret"

def register(username, password):
    url = f"{BASE}/auth/register"
    payload = {"username": username, "password": password}
    headers = {"Content-Type": "application/json"}
    r = requests.post(url, json=payload, headers=headers, timeout=10)
    try:
        j = r.json()
    except Exception:
        print("Non-JSON response:", r.text)
        r.raise_for_status()
    print("Status:", r.status_code)
    print(json.dumps(j, indent=2))
    return j

if __name__ == "__main__":
    u = USERNAME
    p = PASSWORD
    if len(sys.argv) >= 3:
        u = sys.argv[1]; p = sys.argv[2]
    register(u, p)
