# app/websocket_manager.py

from typing import List, Dict, Callable
from fastapi import WebSocket
import json
import asyncio

class WebSocketManager:
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}  # report_id -> connections
        self.ping_interval = 25  # Heroku's timeout is 55 seconds, so ping every 25 seconds
        self.message_handlers: List[Callable] = []  # Add this line for message handlers

    async def connect(self, websocket: WebSocket, report_id: str):
        await websocket.accept()
        if report_id not in self.active_connections:
            self.active_connections[report_id] = []
        self.active_connections[report_id].append(websocket)

    def disconnect(self, websocket: WebSocket, report_id: str):
        if report_id in self.active_connections:
            if websocket in self.active_connections[report_id]:
                self.active_connections[report_id].remove(websocket)
            if not self.active_connections[report_id]:
                del self.active_connections[report_id]

    async def broadcast_to_report(self, report_id: str, message: str):
        if report_id in self.active_connections:
            for connection in self.active_connections[report_id]:
                try:
                    await connection.send_text(message)
                except Exception as e:
                    print(f"Error sending message: {e}")
                    await self.disconnect(connection, report_id)
        
        # Add this: Notify all message handlers
        try:
            for handler in self.message_handlers:
                await handler(message)
        except Exception as e:
            print(f"Error notifying message handlers: {e}")

    async def keep_alive(self, websocket: WebSocket):
        while True:
            try:
                await asyncio.sleep(self.ping_interval)
                await websocket.send_text('ping')
            except Exception:
                break

    def add_handler(self, handler: Callable):
        """Add a message handler function"""
        if handler not in self.message_handlers:
            self.message_handlers.append(handler)

    def remove_handler(self, handler: Callable):
        """Remove a message handler function"""
        if handler in self.message_handlers:
            self.message_handlers.remove(handler)

# Create an instance of WebSocketManager to use in your service
websocket_manager = WebSocketManager()