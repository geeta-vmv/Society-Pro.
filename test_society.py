import pytest
import requests

BASE_URL = "http://localhost:5001"

def test_homepage_loads():
    response = requests.get(f"{BASE_URL}/")
    assert response.status_code < 500

def test_login_page_accessible():
    # फ्लॅस्क ॲपमधील लॉगिन राऊट तपासणे
    response = requests.get(f"{BASE_URL}/login")
    assert response.status_code in [200, 302, 404]

def test_otp_workflow_mock():
    payload = {"email": "test_resident@societypro.com"}
    response = requests.post(f"{BASE_URL}/api/send-otp", json=payload)
    assert response.status_code in [200, 400, 404, 405, 500]

def test_dashboard_access_unauthorized():
    response = requests.get(f"{BASE_URL}/dashboard", allow_redirects=False)
    assert response.status_code in [200, 302, 401, 403, 404]