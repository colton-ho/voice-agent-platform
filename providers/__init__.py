"""Provider-neutral contracts package (implementation-plan §3, U-002).

契约层只用标准库——任何 pipecat/AWS/Azure SDK 类型都不得出现在此包内
（适配器在边界做翻译，契约不受 SDK 漂移影响，G-1）。
"""
