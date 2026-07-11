import hashlib


def notification_message_id(channel: str, message: str, icon: str, timestamp: str) -> str:
    payload = "\0".join([channel or "default", message or "", icon or "info", timestamp or ""])
    return "n" + hashlib.sha1(payload.encode("utf-8", errors="ignore")).hexdigest()[:24]
