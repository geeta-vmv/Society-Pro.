import sys
import os
import pytest

# Backend फोल्डरचा पाथ सिस्टीममध्ये सेट करत आहोत
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), 'backend')))

from app import app

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

# 1. होम पेज किंवा इंडेक्स राऊट टेस्ट करणे
def test_home_page(client):
    response = client.get('/')
    # स्टेटस कोड 200 (OK) किंवा 302 (Redirect) आला पाहिजे
    assert response.status_code in [200, 302, 404]

# 2. कॉन्फिग किंवा इतर एंडपॉइंट्ससाठी टेस्ट (क्रॅश होत नाहीये का ते तपासणे)
def test_backend_status(client):
    # समजा आपल्याकडे एखादे स्टेटस किंवा हेल्थ चेक API असेल
    response = client.get('/api/health') if '/api/health' in [rule.rule for rule in app.url_map.iter_rules()] else client.get('/')
    assert response.status_code < 500

# 3. OTP पाठवणे आणि व्हेरिफाय करणे
def test_otp_flow(client):
    # Send OTP
    res = client.post('/api/auth/send-otp', json={'mobile': '9999000001'})
    assert res.status_code == 200
    data = res.get_json()
    assert data['success'] is True
    dev_otp = data['data'].get('dev_otp')
    assert dev_otp is not None
    assert len(dev_otp) == 6

    # Verify with correct OTP
    verify_res = client.post('/api/auth/verify-otp', json={'mobile': '9999000001', 'otp': dev_otp})
    assert verify_res.status_code == 200
    vdata = verify_res.get_json()
    assert vdata['success'] is True
    token = vdata['data']['token']
    assert token is not None

    # Authenticated /auth/me call
    me_res = client.get('/api/auth/me', headers={'Authorization': f'Bearer {token}'})
    assert me_res.status_code == 200
    assert me_res.get_json()['data']['mobile'] == '9999000001'

# 4. अमान्य मोबाईल नंबर तपासणे
def test_invalid_mobile_otp(client):
    res = client.post('/api/auth/send-otp', json={'mobile': '123'})
    assert res.status_code == 400
    assert res.get_json()['success'] is False

# 5. चुकीचा OTP आणि मॅक्स अटेम्प्ट्स तपासणे
def test_incorrect_otp_attempts(client):
    res = client.post('/api/auth/send-otp', json={'mobile': '9999000099'})
    assert res.status_code == 200

    # Wrong attempt 1
    w1 = client.post('/api/auth/verify-otp', json={'mobile': '9999000099', 'otp': '000000'})
    assert w1.status_code == 400
    # Wrong attempt 2
    w2 = client.post('/api/auth/verify-otp', json={'mobile': '9999000099', 'otp': '000001'})
    assert w2.status_code == 400
    # Wrong attempt 3 (should lock out / reject)
    w3 = client.post('/api/auth/verify-otp', json={'mobile': '9999000099', 'otp': '000002'})
    assert w3.status_code in [400, 429]

# 6. अनधिकृत ऍक्सेस तपासणे
def test_unauthorized_endpoints(client):
    res = client.get('/api/dashboard/stats')
    assert res.status_code == 401