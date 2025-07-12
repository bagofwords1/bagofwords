import hmac
import hashlib
import time
import httpx
from typing import Dict, Any, Optional
from .base_adapter import PlatformAdapter
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from app.models.external_user_mapping import ExternalUserMapping
from app.models.organization import Organization
from app.models.user import User
from app.schemas.external_user_mapping_schema import ExternalUserMappingCreate
from app.services.external_user_mapping_service import ExternalUserMappingService
from app.settings.config import settings

class SlackAdapter(PlatformAdapter):
    """Slack platform adapter"""
    
    def __init__(self, platform):
        super().__init__(platform)
        self.bot_user_id = None  # Will be set when needed
    
    async def get_bot_user_id(self) -> str:
        """Get the bot's user ID to filter out bot messages"""
        if self.bot_user_id:
            return self.bot_user_id
        
        try:
            bot_token = self.credentials.get("bot_token")
            if not bot_token:
                return None
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://slack.com/api/auth.test",
                    headers={"Authorization": f"Bearer {bot_token}"}
                )
                
                if response.status_code == 200:
                    result = response.json()
                    if result.get("ok"):
                        self.bot_user_id = result.get("user_id")
                        return self.bot_user_id
                
                return None
                
        except Exception as e:
            print(f"Error getting bot user ID: {e}")
            return None
    
    async def process_incoming_message(self, event_data: Dict[str, Any]) -> Dict[str, Any]:
        """Process incoming Slack message"""
        
        # Extract relevant data from Slack event
        event = event_data.get("event", {})
        
        # Get bot user ID to filter out bot messages
        bot_user_id = await self.get_bot_user_id()
        
        # Check if this is a bot message
        if event.get("user") == bot_user_id:
            print("Ignoring bot message in adapter")
            return None
        
        return {
            "platform_type": "slack",
            "external_user_id": event.get("user"),
            "external_message_id": event.get("ts"),
            "channel_id": event.get("channel"),
            "message_text": event.get("text", ""),
            "message_type": event.get("type"),
            "timestamp": event.get("ts"),
            "team_id": event_data.get("team_id")
        }
    
    async def send_response(self, message_data: Dict[str, Any]) -> bool:
        """Send response to Slack"""
        
        try:
            bot_token = self.credentials.get("bot_token")
            if not bot_token:
                print("No bot token available")
                return False
            
            # Format message for Slack
            slack_message = {
                "channel": message_data.get("channel") or message_data.get("channel_id"),
                "text": message_data.get("content", "") or message_data.get("text", ""),
            }
            
            # Add thread_ts if provided
            if message_data.get("thread_ts"):
                slack_message["thread_ts"] = message_data.get("thread_ts")
            
            # Add blocks if provided (for rich messages)
            if message_data.get("blocks"):
                slack_message["blocks"] = message_data.get("blocks")
            
            print(f"Sending Slack message: {slack_message}")
            
            # Validate required fields
            if not slack_message["channel"]:
                print("Error: No channel specified")
                return False
            
            if not slack_message["text"] and not slack_message.get("blocks"):
                print("Error: No text or blocks specified")
                return False
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    "https://slack.com/api/chat.postMessage",
                    headers={
                        "Authorization": f"Bearer {bot_token}",
                        "Content-Type": "application/json"
                    },
                    json=slack_message
                )
                
                print(f"Slack API response status: {response.status_code}")
                
                if response.status_code == 200:
                    result = response.json()
                    print(f"Slack API response: {result}")
                    return result.get("ok", False)
                else:
                    print(f"Slack API error: {response.status_code} - {response.text}")
                    return False
                    
        except httpx.ConnectTimeout:
            print("Slack API connection timeout")
            return False
        except httpx.ReadTimeout:
            print("Slack API read timeout")
            return False
        except Exception as e:
            print(f"Error sending Slack message: {e}")
            return False
    
    async def get_user_info(self, external_user_id: str) -> Dict[str, Any]:
        """Get Slack user information"""
        
        try:
            bot_token = self.credentials.get("bot_token")
            if not bot_token:
                return {}
            
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"https://slack.com/api/users.info?user={external_user_id}",
                    headers={"Authorization": f"Bearer {bot_token}"}
                )
                
                if response.status_code == 200:
                    result = response.json()
                    if result.get("ok"):
                        user_info = result.get("user", {})
                        return {
                            "id": user_info.get("id"),
                            "name": user_info.get("name"),
                            "email": user_info.get("profile", {}).get("email"),
                            "real_name": user_info.get("profile", {}).get("real_name")
                        }
                
                return {}
                
        except Exception as e:
            print(f"Error getting Slack user info: {e}")
            return {}
    
    async def verify_webhook_signature(self, request_body: bytes, signature: str, timestamp: str) -> bool:
        """Verify Slack webhook signature"""
        
        try:
            signing_secret = self.credentials.get("signing_secret")
            if not signing_secret:
                return False
            
            # Create signature
            sig_basestring = f"v0:{timestamp}:{request_body.decode('utf-8')}"
            expected_signature = f"v0={hmac.new(signing_secret.encode(), sig_basestring.encode(), hashlib.sha256).hexdigest()}"
            
            return hmac.compare_digest(expected_signature, signature)
            
        except Exception as e:
            print(f"Error verifying Slack signature: {e}")
            return False
    
    async def send_verification_message(self, channel_id: str, email: str, token: str) -> bool:
        """Send verification message to Slack user"""
        
        try:
            # Use the configured base URL from settings
            base_url = settings.bow_config.base_url
            verification_url = f"{base_url}/settings/integrations/verify/{token}"
            
            message = {
                "channel": channel_id,
                "text": f"To start using this bot, please verify your account by clicking this link: <{verification_url}|Verify Account>",
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Account Verification Required*\n\nTo start using this bot, please click the button below to verify your account: <{verification_url}>"
                        }
                    }
                ]
            }
            
            print(f"Sending verification message to channel {channel_id}")
            print(f"Verification URL: {verification_url}")
            return await self.send_response(message)
            
        except Exception as e:
            print(f"Error sending verification message: {e}")
            return False

    async def send_dm(self, user_id: str, text: str) -> bool:
        """
        Open a DM with the user and send a message using send_response.
        """
        bot_token = self.credentials.get("bot_token")
        if not bot_token:
            print("No bot token available")
            return False

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                # Open a DM channel
                open_resp = await client.post(
                    "https://slack.com/api/conversations.open",
                    headers={
                        "Authorization": f"Bearer {bot_token}",
                        "Content-Type": "application/json"
                    },
                    json={"users": user_id}
                )
                open_data = open_resp.json()
                if not open_data.get("ok"):
                    print(f"Failed to open DM: {open_data}")
                    return False
                channel_id = open_data["channel"]["id"]

                # Use your existing send_response method
                return await self.send_response({
                    "channel": channel_id,
                    "text": text
                })
        except Exception as e:
            print(f"Error sending DM: {e}")
            return False
