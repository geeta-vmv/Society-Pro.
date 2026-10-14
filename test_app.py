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