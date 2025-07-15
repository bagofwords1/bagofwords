import os
import sys
import logging
import requests
import json
import pandas as pd

# Configure basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class APIClient:
    def __init__(self, base_url="http://localhost:8000"):
        self.base_url = base_url
        self.token = None
        self.org_id = None

    def login(self, email, password):
        """Login and get JWT token"""
        try:
            response = requests.post(
                f"{self.base_url}/api/auth/jwt/login",
                data={
                    "username": email,
                    "password": password
                }
            )
            response.raise_for_status()
            self.token = response.json().get("access_token")
            logger.info("Successfully logged in")
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Login failed: {str(e)}")
            sys.exit(1)

    def get_organizations(self):
        """Get user's organizations"""
        if not self.token:
            logger.error("Token not set. Please login first.")
            return None

        headers = {
            "Authorization": f"Bearer {self.token}"
        }

        try:
            response = requests.get(
                f"{self.base_url}/api/organizations",
                headers=headers
            )
            response.raise_for_status()
            orgs = response.json()
            if orgs and len(orgs) > 0:
                self.org_id = orgs[0].get("id")  # Get the first (default) org
                logger.info(f"Using organization ID: {self.org_id}")
            return orgs
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to get organizations: {str(e)}")
            return None

    def create_report(self, title="Test Report"):
        """Create a new report"""
        if not self.token or not self.org_id:
            logger.error("Token or org_id not set. Please login first.")
            return None

        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Organization-Id": str(self.org_id)
        }

        payload = {
            "title": title,
            "widget": None,
            "files": [],
            "data_sources": []
        }

        try:
            response = requests.post(
                f"{self.base_url}/api/reports",
                json=payload,
                headers=headers
            )
            response.raise_for_status()
            logger.info("Successfully created report")
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Report creation failed: {str(e)}")
            return None

    def create_completion(self, report_id: str, prompt: str):
        """Create a completion request"""
        if not self.token or not self.org_id:
            logger.error("Token or org_id not set. Please login first.")
            return None

        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Organization-Id": str(self.org_id)
        }

        payload = {
            "prompt": {
                "content": prompt,
                "widget_id": None,
                "step_id": None,
                "mentions": [{}]
            }
        }

        try:
            response = requests.post(
                f"{self.base_url}/api/reports/{report_id}/completions",
                json=payload,
                headers=headers,
                params={"background": False}
            )
            response.raise_for_status()
            logger.info("Successfully created completion")
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Completion request failed: {str(e)}")
            return None

def main():
    # Initialize client
    client = APIClient()

    # Configuration
    EMAIL = "admin@email.com"
    PASSWORD = "password"

    # create a report via UI and get the id from the URL
    REPORT_ID = 'report-uuid'
    
    # 1. Login
    client.login(EMAIL, PASSWORD)
    
    # 2. Get organizations and use the default one
    client.get_organizations()
    
    prompts = [
        "top 10 customers in asia by spend, in descending order"
    ]

    for prompt in prompts:
        completions = client.create_completion(REPORT_ID, prompt)
        
        if completions:

            for completion in completions:
                if not completion.get('step'):
                    continue

                step = completion.get('step', {})
                data = step.get('data', {})

                if step.get('code'):
                    print(f"\nPrompt: {prompt}")
                    print(f"Reasoning: {completion.get('reasoning', 'No reasoning provided')}")
                    print(f"Completion: {completion.get('completion', {}).get('content', 'No completion content')}")
                    print(f"Title: {step.get('title', 'No title provided')}")
                    print(f"Code: {step.get('code', 'No code provided')}")
                    
                    rows = data.get('rows', [])
                    cols = data.get('columns', [])
                    if rows and cols:
                        df = pd.DataFrame(rows, columns=[c['headerName'] for c in cols])
                        print("\nResults:")
                        print(df.head(3))
                    else:
                        print("\nNo data available")

if __name__ == "__main__":
    main()

