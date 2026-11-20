"""FC-1 telephony adapter package (Twilio first).

U-201 落地 Twilio 的 webhook + WS-upgrade 签名层；后续单元（U-202..U-207）在此
扩展媒体/转接/挂断等 FC-1 能力。此包保持导入纯度：不引 boto3/aioboto3，
签名后端优先官方 twilio SDK、缺失时回退 stdlib ``twilio_sig``（见 A-1）。
"""
from providers.telephony.twilio_adapter import (
    TwilioConfig,
    TwilioSignatureValidator,
    build_twiml_connect_stream,
    validate_incoming_call,
)

__all__ = [
    "TwilioConfig",
    "TwilioSignatureValidator",
    "build_twiml_connect_stream",
    "validate_incoming_call",
]
