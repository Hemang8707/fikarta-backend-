import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))
from main import app
from fastapi.testclient import TestClient

class TestFikartaBackendAPIs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_01_health_and_stats(self):
        res = self.client.get("/api/v1/health")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "healthy")

        res_stats = self.client.get("/api/v1/stats")
        self.assertEqual(res_stats.status_code, 200)
        self.assertGreater(res_stats.json()["total_programmatic_entities"], 10000)

    def test_02_dynamic_resolver(self):
        routes = [
            ("/nbfc/bajaj-finance-ltd", "nbfc"),
            ("/loan-apps/kreditbee", "loan_app"),
            ("/emi-calculator/personal-loan", "calculator"),
            ("/news/1-crore-by-50-your-sip-amount-by-age-20260905", "news"),
            ("/blog/1-missed-emi-lender-may-lock-your-phone-soon", "blog"),
            ("/hi/cibil-score-kaise-badhaye", "hindi"),
            ("/personal-loan/mumbai", "city"),
            ("/loan-amount/50000", "amount"),
            ("/loan-kavach/cibil-after-settlement", "kavach"),
            ("/tips/10000-loan-emi-options", "tip"),
            ("/glossary/foir", "glossary"),
            ("/lenders/hdfc-bank", "lender"),
            ("/personal-loan-for/women", "purpose"),
            ("/personal-loan/salary/20000", "salary"),
            ("/cibil-score/550-loan-app", "cibil"),
            ("/ai-advisor/ai-advisor", "advisor"),
            ("/compare/hdfc-bank-vs-sbi", "comparison"),
            ("/research/ai-personal-finance-india-2026-landscape-report", "research"),
        ]
        for route, expected_type in routes:
            res = self.client.get(f"/api/v1/resolve?path={route}")
            self.assertEqual(res.status_code, 200, f"Route {route} failed")
            self.assertEqual(res.json().get("route_type"), expected_type)

    def test_03_universal_search(self):
        queries = ["bajaj", "women", "20000", "cibil", "settlement", "sbi"]
        for q in queries:
            res = self.client.get(f"/api/v1/search?q={q}")
            self.assertEqual(res.status_code, 200)
            self.assertGreater(res.json()["total"], 0)

    def test_04_all_entity_endpoints(self):
        endpoints = [
            ("/api/v1/cities", "/api/v1/cities/mumbai"),
            ("/api/v1/nbfc", "/api/v1/nbfc/bajaj-finance-ltd"),
            ("/api/v1/loan-apps", "/api/v1/loan-apps/kreditbee"),
            ("/api/v1/calculators", "/api/v1/calculators/personal-loan"),
            ("/api/v1/loan-amounts", "/api/v1/loan-amounts/50000"),
            ("/api/v1/loan-kavach", "/api/v1/loan-kavach/cibil-after-settlement"),
            ("/api/v1/news", "/api/v1/news/1-crore-by-50-your-sip-amount-by-age-20260905"),
            ("/api/v1/hindi", "/api/v1/hindi/cibil-score-kaise-badhaye"),
            ("/api/v1/blog", "/api/v1/blog/1-missed-emi-lender-may-lock-your-phone-soon"),
            ("/api/v1/tips", "/api/v1/tips/10000-loan-emi-options"),
            ("/api/v1/glossary", "/api/v1/glossary/foir"),
            ("/api/v1/lenders", "/api/v1/lenders/hdfc-bank"),
            ("/api/v1/research", "/api/v1/research/ai-personal-finance-india-2026-landscape-report"),
            ("/api/v1/purposes", "/api/v1/purposes/women"),
            ("/api/v1/salaries", "/api/v1/salaries/20000"),
            ("/api/v1/cibil", "/api/v1/cibil/550-loan-app"),
            ("/api/v1/advisor", "/api/v1/advisor/ai-advisor"),
            ("/api/v1/comparisons", "/api/v1/compare/hdfc-bank-vs-sbi"),
        ]
        for list_ep, detail_ep in endpoints:
            res_l = self.client.get(list_ep)
            self.assertEqual(res_l.status_code, 200, f"List {list_ep} failed")
            res_d = self.client.get(detail_ep)
            self.assertEqual(res_d.status_code, 200, f"Detail {detail_ep} failed")

    def test_05_interactive_submissions(self):
        # Lead
        res_lead = self.client.post("/api/v1/leads", json={
            "name": "Rohan Mehra",
            "phone": "9811223344",
            "loan_amount": 150000.0,
            "city": "Delhi",
            "consent": True
        })
        self.assertEqual(res_lead.status_code, 201)
        self.assertEqual(res_lead.json()["status"], "success")

        # Contact
        res_contact = self.client.post("/api/v1/contact", json={
            "name": "Kavita Rao",
            "mobile": "9822334455",
            "email": "kavita.rao@example.com",
            "topic": "Grievance Redressal Escalation",
            "message": "Need assistance filing an ombudsman grievance against unsolicited recovery calls."
        })
        self.assertEqual(res_contact.status_code, 201)
        self.assertEqual(res_contact.json()["status"], "success")

        # Scam Report
        res_scam = self.client.post("/api/v1/report-scam", json={
            "app_name": "QuickRupeeFast",
            "issue_type": "Calls to contacts without consent",
            "description": "Blackmail calls from unregistered recovery agents."
        })
        self.assertEqual(res_scam.status_code, 201)
        self.assertEqual(res_scam.json()["status"], "success")

        # Newsletter
        res_sub = self.client.post("/api/v1/subscribe", json={
            "email": "borrower.shield@fikarta.money",
            "source": "unit_test"
        })
        self.assertEqual(res_sub.status_code, 201)
        self.assertEqual(res_sub.json()["status"], "success")

if __name__ == "__main__":
    unittest.main()

