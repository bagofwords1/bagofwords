import base64
import hashlib
import os
import time
import uuid

from typing import Optional, Tuple, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

router = APIRouter()


