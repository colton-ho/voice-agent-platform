"""Azure Speech STT/TTS 适配器包（U-101，implementation-plan §5 Phase B）。

与 providers/ 契约层不同：适配器允许在边界翻译 SDK 类型，但 SDK 导入必须懒加载
（pipecat-ai[azure] 不在部署钉里，见 azure_speech.py 模块注）。
"""
